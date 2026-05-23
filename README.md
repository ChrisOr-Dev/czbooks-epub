# czbooks_epub

Convert a [czbooks.net](https://czbooks.net/) novel's index page into an EPUB,
either through a browser (web UI) or from the command line.

> **Live demo:** [books.chiiapp.com](https://books.chiiapp.com) — public instance, rate-limited.

- **Browser flow**: paste URL → pick chapter range → live progress → EPUB downloads.
  The EPUB is assembled in your browser (JSZip), so the server never stores files.
- **CLI flow**: the original `main.py` still works for scripted use.
- Requests-based scraper; no Playwright required for the happy path.
  Docker image is about 200 MB.
- Multi-user friendly: IP-based rate limit, ephemeral in-memory job queue.

## Features

- **Fast scraper**: `requests` + `ThreadPoolExecutor` (default 10 parallel).
  A 500-chapter book finishes in roughly 30 seconds.
- **Client-side EPUB build**: the server streams chapter content over SSE and
  the browser assembles a valid EPUB 3 (cover, nav, NCX) with JSZip. No
  server-side EPUB storage, no cleanup job, no download endpoint to harden.
- **Playwright fallback**: kept in the dev image to handle a hypothetical
  future where czbooks goes JS-rendered. Production image ships without it.
- **Web UI**: Flask + SSE progress, plain JS + TailwindCSS, dark theme.
- **Job queue**: ThreadPoolExecutor, 4 concurrent jobs by default, extras queue.
- **Rate limit**: Flask-Limiter, 5 jobs per IP per hour (configurable).
- **Container-friendly**: 512 MB memory limit; idles around 50 MB.
- **Mobile-aware**: requests a screen Wake Lock during the download so the
  phone doesn't sleep mid-stream and drop the SSE connection.
- **Robust ordering**: detects czbooks' "latest chapter" shortcut anchor and
  moves it to the end so chapter 1 appears first, not a mid-book chapter.

## Quick start

### Local dev (Python)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# Open http://localhost:5050
```

### Docker

```bash
docker compose up --build
# Open http://localhost:5050
```

### CLI

```bash
python main.py https://czbooks.net/n/xxxxx --chapters 1-50 -o novel.epub
python main.py https://czbooks.net/n/xxxxx --test       # parse index only
```

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_JOBS` | `4` | Number of scrape jobs that may run in parallel |
| `PER_JOB_CONCURRENCY` | `10` | Chapter-level threads inside a single job |
| `RATE_LIMIT` | `5 per hour` | Limit applied to `POST /api/jobs` per client IP |
| `TZ` | `America/Toronto` | Container timezone |

## HTTP API

| Method | Path | Description |
|---|---|---|
| `GET`  | `/` | Web UI |
| `POST` | `/api/parse` | Parse index page; returns title, author, cover URL, chapter list |
| `GET`  | `/api/cover-proxy?url=...` | Fetch a czbooks cover image with CORS headers |
| `POST` | `/api/jobs` | Submit a download job; returns `job_id` |
| `GET`  | `/api/jobs/<id>/events` | SSE stream: `status` / `novel` / `chapter` / `progress` / `done` / `error` |
| `GET`  | `/healthz` | Health probe |

The browser is expected to subscribe to `/events` before the first chapter
arrives (the events are not replayed for late subscribers).

## Architecture

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

```
Browser ──HTTPS──► reverse proxy / tunnel ──► host:5050 ─► Flask app
                                                            ├─ Job queue (ThreadPoolExecutor)
                                                            ├─ Scraper (requests + threads)
                                                            └─ SSE chapter stream
                                                              ▲
                                                              │ chapter events
Browser ──── JSZip → EPUB blob ──── download ───────────────┘
```

## Deployment

`docker-compose.yml` uses a bridge network on port 5050 with a 512 MB memory
limit. Put a reverse proxy (Cloudflare Tunnel, Nginx, Caddy, Traefik, etc.)
in front of `http://localhost:5050` for HTTPS.

```bash
./scripts/deploy.sh deploy    # git pull, rebuild, restart
./scripts/deploy.sh logs      # follow logs
./scripts/deploy.sh restart   # restart container
./scripts/deploy.sh stop      # docker compose down
```

## Disclaimer

This tool is for personal offline reading. Please respect the original
authors' copyright — do not redistribute or commercialize the downloaded
content. The scraper may need updates if the source site changes structure.

## License

MIT — see [LICENSE](LICENSE).
