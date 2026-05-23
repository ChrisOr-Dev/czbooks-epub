"""
Flask backend for czbooks → EPUB web app.

Server scrapes chapter HTML and streams plain content to the browser via SSE;
the browser assembles the EPUB locally (JSZip). The server therefore never
holds an EPUB file on disk — no /tmp, no cleanup, no download endpoint.

Routes:
  GET  /                          → index.html
  POST /api/parse                 → parse novel index, return title/author/cover/chapter list
  GET  /api/cover-proxy?url=...   → fetch a czbooks cover with CORS headers
  POST /api/jobs                  → enqueue a job, return job_id
  GET  /api/jobs/<id>/events      → SSE stream: status / novel / chapter / done / error
  GET  /healthz                   → health check
"""
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from scraper import HEADERS, Scraper

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", 4))
PER_JOB_CONCURRENCY = int(os.environ.get("PER_JOB_CONCURRENCY", 10))
RATE_LIMIT = os.environ.get("RATE_LIMIT", "5 per hour")
ALLOWED_HOST_SUFFIX = "czbooks.net"
COVER_MAX_BYTES = 5 * 1024 * 1024
MAX_JOBS_RETAINED = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    url: str
    start: int
    end: int
    concurrency: int
    status: str = "queued"          # queued | running | done | error
    progress: int = 0
    total: int = 0
    last_title: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    events: "queue.Queue[dict]" = field(default_factory=queue.Queue)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, url: str, start: int, end: int, concurrency: int) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            url=url,
            start=start,
            end=end,
            concurrency=concurrency,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._trim_locked()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def _trim_locked(self) -> None:
        if len(self._jobs) <= MAX_JOBS_RETAINED:
            return
        items = sorted(self._jobs.values(), key=lambda j: j.created_at)
        to_remove = len(items) - MAX_JOBS_RETAINED
        for job in items:
            if to_remove <= 0:
                break
            if job.status in ("done", "error"):
                self._jobs.pop(job.id, None)
                to_remove -= 1
        if to_remove > 0:
            items = sorted(self._jobs.values(), key=lambda j: j.created_at)
            for job in items[:to_remove]:
                self._jobs.pop(job.id, None)


jobs = JobManager()
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="job-")

# ---------------------------------------------------------------------------
# Job worker — streams chapter content; never writes an EPUB
# ---------------------------------------------------------------------------


def _push_event(job: Job, event: str, data: dict) -> None:
    job.events.put({"event": event, "data": data})


def _run_job(job: Job) -> None:
    logger.info(f"job {job.id} starting: {job.url} [{job.start}-{job.end}]")
    job.status = "running"
    _push_event(job, "status", {"status": "running"})

    try:
        scraper = Scraper(concurrency=job.concurrency)
        novel = scraper.parse_novel_index(job.url)
        if not novel or not novel.chapters:
            raise RuntimeError("Failed to parse novel index or no chapters found")

        total_chapters = len(novel.chapters)
        start = max(1, min(job.start, total_chapters))
        end = max(start, min(job.end, total_chapters))
        job.start, job.end = start, end
        job.total = end - start + 1

        _push_event(job, "novel", {
            "title": novel.title,
            "author": novel.author,
            "url": novel.url,
            "cover_url": novel.cover_url,
            "total_chapters": total_chapters,
            "range": [start, end],
        })

        # Fetch then stream — fetch_chapters fills .content on each Chapter; we
        # rely on the progress callback to know which one just finished and
        # push it out the SSE channel before moving on (callback runs in the
        # producer thread that called fetch_chapters).
        chapters_subset = novel.chapters[start - 1:end]
        sent: set[int] = set()

        def cb(done: int, total: int, title: str) -> None:
            job.progress = done
            job.last_title = title
            # Identify the chapter that just finished by matching title; if
            # multiple share a title, pick the first not yet sent.
            for i, ch in enumerate(chapters_subset, 1):
                if ch.title == title and i not in sent and ch.content:
                    sent.add(i)
                    _push_event(job, "chapter", {
                        "idx": i,
                        "title": ch.title,
                        "content": ch.content,
                    })
                    break
            _push_event(job, "progress", {"done": done, "total": total, "title": title})

        scraper.fetch_chapters(novel, start=start, end=end, progress_callback=cb)

        # Flush any chapters that finished but had no content (skipped above)
        for i, ch in enumerate(chapters_subset, 1):
            if i not in sent and ch.content:
                _push_event(job, "chapter", {
                    "idx": i,
                    "title": ch.title,
                    "content": ch.content,
                })
                sent.add(i)

        if not sent:
            raise RuntimeError("No chapter content was downloaded")

        job.status = "done"
        _push_event(job, "done", {
            "chapters_included": len(sent),
            "title": novel.title,
            "author": novel.author,
        })
        logger.info(f"job {job.id} done: streamed {len(sent)} chapters")
    except Exception as e:
        logger.exception(f"job {job.id} failed")
        job.status = "error"
        job.error = str(e)
        _push_event(job, "error", {"message": str(e)})
    finally:
        _push_event(job, "_end", {})


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=[],
)


def _validate_url(url: str) -> Optional[str]:
    if not url:
        return "URL is required"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Invalid URL"
    if parsed.scheme not in ("http", "https"):
        return "URL must be http(s)"
    host = (parsed.hostname or "").lower()
    if not host.endswith(ALLOWED_HOST_SUFFIX):
        return f"Only {ALLOWED_HOST_SUFFIX} URLs are allowed"
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/parse", methods=["POST"])
@limiter.limit("20 per hour")
def api_parse():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    err = _validate_url(url)
    if err:
        return jsonify({"error": err}), 400
    scraper = Scraper(concurrency=PER_JOB_CONCURRENCY)
    novel = scraper.parse_novel_index(url)
    if not novel:
        return jsonify({"error": "Failed to parse novel page"}), 502
    if not novel.chapters:
        return jsonify({"error": "No chapters found on that page"}), 422
    return jsonify({
        "title": novel.title,
        "author": novel.author,
        "url": novel.url,
        "cover_url": novel.cover_url,
        "chapter_count": len(novel.chapters),
        "chapters_preview": [
            {"idx": i + 1, "title": ch.title}
            for i, ch in enumerate(novel.chapters[:20])
        ],
    })


def _resolves_to_public_ip(host: str) -> bool:
    """Resolve host and verify every address is a public, routable IP.
    Blocks SSRF to private/loopback/link-local/multicast/reserved ranges."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


@app.route("/api/cover-proxy")
@limiter.limit("60 per hour")
def api_cover_proxy():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    try:
        parsed = urlparse(url)
    except ValueError:
        return jsonify({"error": "invalid url"}), 400
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": "url must be http(s)"}), 400
    host = (parsed.hostname or "").lower()
    if not host:
        return jsonify({"error": "invalid url"}), 400
    if not _resolves_to_public_ip(host):
        return jsonify({"error": "host not allowed"}), 400
    try:
        upstream = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        upstream.raise_for_status()
    except Exception as e:
        logger.warning(f"cover-proxy fetch failed ({url}): {e}")
        return jsonify({"error": "fetch failed"}), 502
    content_type = (upstream.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
        logger.warning(f"cover-proxy rejected non-image content-type {content_type!r} from {url}")
        upstream.close()
        return jsonify({"error": "not an image"}), 415

    def stream():
        total = 0
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > COVER_MAX_BYTES:
                    logger.warning(f"cover-proxy aborted at {total} bytes from {url}")
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(
        stream(),
        content_type=content_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.route("/api/jobs", methods=["POST"])
@limiter.limit(RATE_LIMIT)
def api_create_job():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    err = _validate_url(url)
    if err:
        return jsonify({"error": err}), 400
    try:
        start = int(body.get("start", 1))
        end = int(body.get("end", 0)) or 1
        concurrency = int(body.get("concurrency", PER_JOB_CONCURRENCY))
    except (TypeError, ValueError):
        return jsonify({"error": "start/end/concurrency must be integers"}), 400
    if start < 1 or end < start:
        return jsonify({"error": "Invalid chapter range"}), 400
    concurrency = max(1, min(concurrency, 20))

    job = jobs.create(url=url, start=start, end=end, concurrency=concurrency)
    executor.submit(_run_job, job)
    return jsonify({"job_id": job.id, "status": job.status}), 202


@app.route("/api/jobs/<job_id>/events")
def api_job_events(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

    @stream_with_context
    def stream():
        yield _format_sse("status", {"status": job.status, "progress": job.progress, "total": job.total})
        if job.status == "error":
            yield _format_sse("error", {"message": job.error})
            return
        # Note: chapters are only delivered live; a late subscriber will miss
        # the body events for chapters already streamed. The frontend should
        # subscribe before submitting the job (it does).
        while True:
            try:
                evt = job.events.get(timeout=20)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if evt["event"] == "_end":
                return
            yield _format_sse(evt["event"], evt["data"])

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def _format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.route("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, threaded=True)
