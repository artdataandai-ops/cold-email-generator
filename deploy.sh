#!/usr/bin/env bash
#
# Cold-Email Research Assistant — production deploy script.
#
#   app (uvicorn, 2 workers) :8000   — serves UI at / and API at /api/*
#   Public entrypoint: http(s)://<host>:8000/
#
# Usage:
#   ./deploy.sh           # pull, build, (re)start the stack, wait for health
#   ./deploy.sh --no-pull # skip git pull (deploy the current checkout)
#
set -euo pipefail

cd "$(dirname "$0")"

PULL=1
for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# docker compose v2 (plugin) preferred, fall back to legacy docker-compose.
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "❌ Docker Compose not found. Install Docker first." >&2
  exit 1
fi

# Secrets must exist (injected at runtime via env_file, never baked into the image).
if [ ! -f .env ]; then
  echo "❌ .env is missing. Create it from the template:" >&2
  echo "     cp .env.example .env   # then set OPENAI_API_KEY (+ APIFY_TOKEN for LinkedIn)" >&2
  exit 1
fi

if [ "$PULL" -eq 1 ] && [ -d .git ]; then
  echo "▶ Pulling latest changes..."
  git pull --ff-only
fi

echo "▶ Building image..."
$COMPOSE build

echo "▶ Starting stack..."
$COMPOSE up -d

echo "▶ Waiting for the app to come up..."
ok=0
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:4767/api/health || true)"
  if [ "$code" = "200" ]; then ok=1; break; fi
  sleep 2
done

echo
$COMPOSE ps
echo
if [ "$ok" -eq 1 ]; then
  echo "✅ Deploy complete — health 200 on localhost:4767."
  echo "   Public URL (via edge nginx): https://ai.arttechgroup.com:7777/coldemail/"
  echo "   (Container is mounted under /coldemail; the UI works through the edge, not direct on :4767.)"
else
  echo "⚠️  Stack started but health check did not return 200 in time."
  echo "   Check logs:  $COMPOSE logs -f"
  exit 1
fi
