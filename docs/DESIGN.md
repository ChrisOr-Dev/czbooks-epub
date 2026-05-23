# czbooks_epub Web App 設計文件

把 czbooks.net 小說索引頁轉成 EPUB 的網頁服務。瀏覽器貼 URL、選章節範圍、下載 EPUB；檔案在下載完成後立即刪除。

---

## Context

`czbooks_epub` 原為本地 CLI（`python main.py <url>`）。改造目標：

1. **使用門檻**：命令列對非技術用戶不友善。改為瀏覽器界面：貼 URL → 解析 → 下載。
2. **效能**：原 scraper 每章開 Playwright page（~2s 啟動 + 800ms 寫死 wait + 隨機 jitter）。經實測 czbooks 章節頁是靜態 HTML，純 `requests` 就能拿到完整章節內文。
3. **公開部署**：要能在資源受限的 ARM 主機上跑 Docker，背後接 Cloudflare tunnel 對外提供服務，多用戶能同時用，但要防濫用、自動清理磁碟。

## 設計決策

| 項目 | 決定 |
|---|---|
| 訪問控制 | 公開 + IP rate limit（每 IP 每小時 5 個任務） |
| EPUB 保留 | 下載完立刻刪除；24h 未下載也刪（安全網） |
| 同時任務數 | 2（背景 worker 池） |
| 章節數上限 | 不設上限（靠 Docker memory limit 防 OOM） |
| 容器架構 | 單一 Flask 容器，無 DB/Redis |

---

## Architecture

```
User browser
   │ HTTPS
   ▼
公開網址 (Cloudflare)
   │ Cloudflare tunnel
   ▼
host:5050  (Docker container czbooks_epub_web)
   │
   ├─ Flask app (gunicorn 1 worker + 8 threads)
   ├─ Job queue (ThreadPoolExecutor max_workers=2)
   ├─ Scraper (requests + ThreadPoolExecutor)
   └─ /tmp/epubs/<job_id>.epub  (auto-cleanup)
```

**單容器、無外部依賴**。任務狀態存記憶體（dict + lock），重啟即失（重啟期間若有人提交，回 job not found，可重試）。

---

## Code

### `scraper.py` — requests-first

| 改造前 | 改造後 |
|---|---|
| 每章用 Playwright 開新 page | `requests.Session` 重用連線 + `ThreadPoolExecutor` |
| `_extract_content`：找不到 `<p>` 才走 `<br>` fallback | 偵測 `<br>` 存在即用 `<br>` splitting（修正章節開頭只有 MV 連結 `<p>` 的誤判） |
| `wait_for_timeout(800)` + 隨機 jitter | 拿掉 |
| `wait_until="load"` | 改 `domcontentloaded`（fallback 路徑） |
| 預設 concurrency | 5 → 10 |
| 章節抓取入口 | `fetch_chapters` 改用 requests path；失敗章節收集後走原 Playwright async fallback |

保留 Playwright fallback（索引與章節皆有），happy path 不會觸發。

### `app.py` — Flask 後端

| Method | Path | 用途 |
|---|---|---|
| GET | `/` | 渲染 `templates/index.html` |
| POST | `/api/parse` | body `{url}` → `{title, author, chapter_count, chapters_preview}` |
| POST | `/api/jobs` | body `{url, start, end, concurrency}` → 排入 queue，回 `{job_id}` |
| GET | `/api/jobs/<id>/events` | SSE：`status` / `novel` / `progress` / `done` / `error` |
| GET | `/api/jobs/<id>/download` | 串流 EPUB；串流結束後刪檔 + 從 jobs dict 移除 |

實作要點：

- `JobManager`：dict `{job_id: Job}`，`threading.Lock` 保護，FIFO trim 50 個
- `ThreadPoolExecutor(max_workers=2)` 處理 scrape job
- 每個 job 內部用 `Scraper(concurrency=10)`
- 進度透過 job 內 `queue.Queue` 推給 SSE handler
- 完成後寫 `/tmp/epubs/<job_id>.epub`
- `Flask-Limiter` `5 per hour` 加在 `/api/jobs`（key by `get_remote_address`）
- 走反向代理後 IP 透過 `werkzeug.middleware.proxy_fix.ProxyFix` 還原
- URL 限制 `*.czbooks.net`

### `templates/index.html`

單檔 HTML，原生 JS，TailwindCSS CDN，深色主題。UI 區塊：URL 輸入 / 章節範圍 / 並行度滑桿 / 進度條 + SSE / 自動觸發下載連結。

### `epub_builder.py`

`build_epub(novel, output_path=None, output_dir=None)`：若 `output_dir` 給定，自動建目錄、檔名從標題產生。CLI 行為不變。

### `cleanup.py`

- `cleanup_expired(epubs_dir, max_age_seconds=86400)`：刪超過 24h 的檔案
- `start_background_sweeper`：daemon thread 每 30 分鐘掃一次
- `safe_unlink`：刪檔不丟例外

### `main.py`

不動，CLI 仍可用。

---

## Docker

### `Dockerfile`

- Base：`python:3.11-slim-bookworm`（ARM64 / amd64 自動匹配）
- 不裝 Playwright/Chromium → image ~200MB
- 啟動：`gunicorn -w 1 --threads 8 -k gthread -b 0.0.0.0:5050 --timeout 600 app:app`

### `docker-compose.yml`

預設使用 bridge network、port 5050、memory 512M 限制、TZ + 任務控制環境變數。`./data/epubs` 持久化生成檔（會被 cleanup 清掉，volume 主要供除錯）。

---

## Cloudflare Tunnel（可選）

若要對外公開服務，建議用 Cloudflare Tunnel：在部署主機安裝 `cloudflared`、建立 tunnel、新增 DNS route 指向你的域名、ingress 規則指向 `http://localhost:5050`、註冊為 systemd service。亦可改用其他反向代理（Caddy、Nginx、Traefik 等）；應用本身不依賴特定方案。

---

## Cleanup Strategy

三層保險：

| Trigger | 動作 |
|---|---|
| Download endpoint 串流完成 | `os.unlink(output_path)` + 從 jobs dict 移除 |
| 背景 thread（每 30 分鐘） | 掃 epubs 目錄，刪 mtime > 24h 的檔案 |
| App 啟動時 | 跑一次 `cleanup_expired` |

Jobs dict 也限制最多 50 個（FIFO 淘汰）。

---

## Resource Considerations

| 風險 | 緩解 |
|---|---|
| 大書 OOM | Docker `memory: 512M`；超過則容器被 kill，使用者看 SSE error 可重試 |
| czbooks 反爬 | requests 帶完整 headers；未來被封鎖則自動 fallback Playwright（dev image 含） |
| 多用戶塞爆 | Flask-Limiter 5/hour/IP + 並行任務上限 2 + memory 512M |

---

## Verification

### 本機開發測試

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py            # http://localhost:5050
```

瀏覽器測：貼 czbooks 索引頁 URL、解析、下載 1-3 章。

### Docker 本機測試

```bash
docker compose build
docker compose up
# 開 http://localhost:5050
# 下載後 ls data/epubs → 空
```

### 部署測試項

- 小書（< 100 章）：< 30 秒完成
- 中型書（500 章）：3-5 分鐘
- 同時兩個瀏覽器分頁 → 平行跑；第 3 個排隊
- 第 6 次提交（同 IP）→ 429 rate limit
- 下載後 data/epubs 目錄為空

---

## File Map

| 路徑 | 動作 |
|---|---|
| `scraper.py` | 改造（requests-first） |
| `epub_builder.py` | 加 `output_dir` 參數 |
| `app.py` | 新增 |
| `cleanup.py` | 新增 |
| `templates/index.html` | 新增 |
| `Dockerfile` | 新增 |
| `docker-compose.yml` | 新增 |
| `.dockerignore` / `.gitignore` | 新增 |
| `requirements.txt` | 更新（拔 playwright，加 flask/limiter/gunicorn） |
| `requirements-dev.txt` | 新增（含 playwright） |
| `scripts/deploy.sh` | 新增 |
| `main.py` | 不動 |
