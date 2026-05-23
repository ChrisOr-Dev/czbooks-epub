# czbooks_epub

把 [czbooks.net](https://czbooks.net/) 的小說索引頁轉成 EPUB 的網頁服務 / CLI 工具。

- 瀏覽器界面：貼 URL → 選章節範圍 → 即時進度條 → 下載 EPUB
- 同樣的程式碼可當 CLI 跑：`python main.py <url>`
- 預設純 `requests` 抓章節，無 Playwright；Docker image 約 200MB
- 多用戶、IP rate limit、檔案下載完自動刪除

## Features

- **Fast path：** `requests` + `ThreadPoolExecutor`（預設 10 平行），500 章書約 30 秒
- **Fallback：** Playwright async（dev image 才裝）保留為 JS-rendered 反爬保險
- **Web UI：** Flask + SSE 即時進度，原生 JS + TailwindCSS
- **任務佇列：** ThreadPoolExecutor、預設同時 2 個任務，超過排隊
- **自動清理：** 下載即刪 + 24h TTL 背景掃除 + 啟動時清掃
- **Rate limit：** Flask-Limiter，每 IP 每小時 5 個任務（可調）
- **資源友善：** Docker 預設 512M memory limit，能跑在低階 ARM 主機

## Quick start

### Local dev (Python)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# 開 http://localhost:5050
```

### Docker

```bash
docker compose up --build
# 開 http://localhost:5050
```

### CLI (legacy)

```bash
python main.py https://czbooks.net/n/xxxxx --chapters 1-50 -o novel.epub
python main.py https://czbooks.net/n/xxxxx --test       # 只解析索引
```

## Configuration

環境變數（皆有預設值）：

| 變數 | 預設 | 說明 |
|---|---|---|
| `EPUBS_DIR` | `/tmp/epubs` | EPUB 暫存目錄 |
| `EPUB_TTL_SECONDS` | `86400` | 未下載檔案保留多久 |
| `MAX_CONCURRENT_JOBS` | `2` | 同時跑幾個下載任務 |
| `PER_JOB_CONCURRENCY` | `10` | 每個任務的章節平行抓取數 |
| `RATE_LIMIT` | `5 per hour` | `/api/jobs` IP rate limit |
| `TZ` | `America/Toronto` | 時區 |

## API

| Method | Path | 用途 |
|---|---|---|
| `GET`  | `/` | Web UI |
| `POST` | `/api/parse` | 解析索引頁 |
| `POST` | `/api/jobs` | 建立下載任務 |
| `GET`  | `/api/jobs/<id>/events` | SSE 進度 |
| `GET`  | `/api/jobs/<id>/download` | 下載 EPUB（下載後立即刪除） |
| `GET`  | `/healthz` | 健康檢查 |

## Architecture

完整設計見 [docs/DESIGN.md](docs/DESIGN.md)。

```
Browser ──HTTPS──► reverse proxy / tunnel ──► host:5050 ─► Flask app
                                                            ├─ Job queue (2 concurrent)
                                                            ├─ Scraper (requests + threads)
                                                            └─ /tmp/epubs/*.epub (auto-cleanup)
```

## Deployment

預設 `docker-compose.yml` 用 bridge network、port 5050、512M memory limit，能直接 `docker compose up -d` 跑。外網建議加反向代理 / tunnel（Cloudflare Tunnel、Nginx、Caddy 都可），路由到 `http://localhost:5050`。

```bash
./scripts/deploy.sh deploy    # git pull + 重建 + 啟動
./scripts/deploy.sh logs      # 看 logs
./scripts/deploy.sh restart   # 重啟
./scripts/deploy.sh stop      # 停止
```

## Disclaimer

本工具僅供個人離線閱讀使用。請尊重原作者版權，勿散布或商業利用所下載的內容。網站結構變更時 scraper 可能失效，需配合更新。

## License

MIT — see [LICENSE](LICENSE).
