#!/bin/bash
# Deploy / manage the czbooks_epub web container.
# Run from anywhere; this script cd's to the repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Detect compose v2 plugin ("docker compose") or legacy v1 binary ("docker-compose")
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: neither 'docker compose' plugin nor 'docker-compose' binary found" >&2
  exit 1
fi

cmd="${1:-help}"

case "$cmd" in
  deploy)
    echo "==> Pulling latest code"
    git pull origin main
    echo "==> Building image"
    "${COMPOSE[@]}" build --no-cache
    echo "==> Restarting container"
    "${COMPOSE[@]}" up -d
    echo "==> Done. Tail logs with: $0 logs"
    ;;
  up)
    "${COMPOSE[@]}" up -d
    ;;
  logs)
    "${COMPOSE[@]}" logs -f --tail=200
    ;;
  restart)
    "${COMPOSE[@]}" restart
    ;;
  stop|down)
    "${COMPOSE[@]}" down
    ;;
  ps|status)
    "${COMPOSE[@]}" ps
    ;;
  shell)
    "${COMPOSE[@]}" exec czbooks /bin/bash
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
