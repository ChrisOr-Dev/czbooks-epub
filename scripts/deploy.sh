#!/bin/bash
# Deploy / manage the czbooks_epub web container.
# Run from anywhere; this script cd's to the repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

cmd="${1:-help}"

case "$cmd" in
  deploy)
    echo "==> Pulling latest code"
    git pull origin main
    echo "==> Building image"
    docker compose build --no-cache
    echo "==> Restarting container"
    docker compose up -d
    echo "==> Done. Tail logs with: $0 logs"
    ;;
  up)
    docker compose up -d
    ;;
  logs)
    docker compose logs -f --tail=200
    ;;
  restart)
    docker compose restart
    ;;
  stop|down)
    docker compose down
    ;;
  ps|status)
    docker compose ps
    ;;
  shell)
    docker compose exec czbooks /bin/bash
    ;;
  *)
    cat <<EOF
Usage: $0 <command>

Commands:
  deploy    Pull, rebuild image, restart container
  up        docker compose up -d (no rebuild)
  logs      Follow logs
  restart   Restart container
  stop      docker compose down
  ps        Show container status
  shell     Open shell inside container
EOF
    exit 1
    ;;
esac
