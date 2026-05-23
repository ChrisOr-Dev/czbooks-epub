"""
Flask backend for czbooks → EPUB web app.

Routes:
  GET  /                          → index.html
  POST /api/parse                 → parse novel index, return chapter list
  POST /api/jobs                  → enqueue a download job, return job_id
  GET  /api/jobs/<id>/events      → SSE stream: progress / done / error
  GET  /api/jobs/<id>/download    → stream EPUB, delete file after
"""
import json
import logging
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from cleanup import cleanup_expired, safe_unlink, start_background_sweeper
from epub_builder import build_epub
from scraper import Scraper

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EPUBS_DIR = os.environ.get("EPUBS_DIR", "/tmp/epubs")
EPUB_TTL_SECONDS = int(os.environ.get("EPUB_TTL_SECONDS", 86400))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", 2))
PER_JOB_CONCURRENCY = int(os.environ.get("PER_JOB_CONCURRENCY", 10))
RATE_LIMIT = os.environ.get("RATE_LIMIT", "5 per hour")
ALLOWED_HOST_SUFFIX = "czbooks.net"
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
    output_path: Optional[str] = None
    output_filename: Optional[str] = None
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
        # Remove finished or oldest jobs first
        items = sorted(self._jobs.values(), key=lambda j: j.created_at)
        to_remove = len(items) - MAX_JOBS_RETAINED
        for job in items:
            if to_remove <= 0:
                break
            if job.status in ("done", "error"):
                safe_unlink(job.output_path)
                self._jobs.pop(job.id, None)
                to_remove -= 1
        # If still over, remove oldest regardless
        if to_remove > 0:
            items = sorted(self._jobs.values(), key=lambda j: j.created_at)
            for job in items[:to_remove]:
                safe_unlink(job.output_path)
                self._jobs.pop(job.id, None)


jobs = JobManager()
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="job-")

# ---------------------------------------------------------------------------
# Job worker
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
            "total_chapters": total_chapters,
            "range": [start, end],
        })

        def cb(done: int, total: int, title: str) -> None:
            job.progress = done
            job.last_title = title
            _push_event(job, "progress", {"done": done, "total": total, "title": title})

        scraper.fetch_chapters(novel, start=start, end=end, progress_callback=cb)

        fetched = [ch for ch in novel.chapters[start - 1:end] if ch.content]
        if not fetched:
            raise RuntimeError("No chapter content was downloaded")

        from copy import copy
        novel_copy = copy(novel)
        novel_copy.chapters = fetched

        output_filename = f"{job.id}.epub"
        output_path = os.path.join(EPUBS_DIR, output_filename)
        build_epub(novel_copy, output_path=output_path)

        job.output_path = output_path
        job.output_filename = f"{novel.title}.epub"
        job.status = "done"
        _push_event(job, "done", {
            "download_url": f"/api/jobs/{job.id}/download",
            "filename": job.output_filename,
            "chapters_included": len(fetched),
        })
        logger.info(f"job {job.id} done: {len(fetched)} chapters → {output_path}")
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
    """Return error message if url is invalid, else None."""
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
        "chapter_count": len(novel.chapters),
        "chapters_preview": [
            {"idx": i + 1, "title": ch.title}
            for i, ch in enumerate(novel.chapters[:20])
        ],
    })


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
        # Replay current state so a late subscriber doesn't miss "done"
        yield _format_sse("status", {"status": job.status, "progress": job.progress, "total": job.total})
        if job.status == "done":
            yield _format_sse("done", {
                "download_url": f"/api/jobs/{job.id}/download",
                "filename": job.output_filename,
            })
            return
        if job.status == "error":
            yield _format_sse("error", {"message": job.error})
            return
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


@app.route("/api/jobs/<job_id>/download")
def api_job_download(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.status != "done" or not job.output_path or not os.path.exists(job.output_path):
        return jsonify({"error": "epub not ready"}), 409

    path = job.output_path
    download_name = job.output_filename or f"{job_id}.epub"

    def generate():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            safe_unlink(path)
            jobs.drop(job_id)
            logger.info(f"job {job_id} downloaded and cleaned up")

    # RFC 5987 filename* for unicode
    from urllib.parse import quote
    encoded = quote(download_name)
    return Response(
        generate(),
        mimetype="application/epub+zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}",
            "Cache-Control": "no-store",
        },
    )


@app.route("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

os.makedirs(EPUBS_DIR, exist_ok=True)
cleanup_expired(EPUBS_DIR, EPUB_TTL_SECONDS)
start_background_sweeper(EPUBS_DIR, EPUB_TTL_SECONDS)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, threaded=True)
