#!/usr/bin/env bash
# Deploys (or updates) the stack on the droplet from your computer:
#   ./deploy.sh root@203.0.113.10 [/path/to/call-center-frontend]
# `--backend-only` instead of the frontend folder deploys just the backend (the dashboard is then
# hosted elsewhere, e.g. Railway, and calls this API).
# Copies the backend and the dashboard to the server, builds the images there, starts the stack
# and waits for the API to be healthy. Re-run it for every update; data is kept.
# BUILD_ONLY=1 ./deploy.sh ... builds the images without starting them (do this while DNS is
# still propagating; start once the three domain names resolve, or HTTPS certificates fail).
set -euo pipefail
TARGET="${1:?usage: ./deploy.sh user@host [frontend-dir | --backend-only]}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(cd "$HERE/../.." && pwd)"
if [ "${2:-}" = "--backend-only" ]; then WITH_FRONTEND=0; FRONTEND=""; else WITH_FRONTEND=1; FRONTEND="${2:-$BACKEND/../call-center-frontend}"; fi
PROFILE=""; [ "$WITH_FRONTEND" = 1 ] && PROFILE="--profile frontend"
REMOTE=/opt/callpulse
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)

[ "$WITH_FRONTEND" = 0 ] || [ -d "$FRONTEND" ] || { echo "frontend folder not found: $FRONTEND" >&2; exit 1; }
ssh "${SSH_OPTS[@]}" "$TARGET" "test -f $REMOTE/app/deploy/digitalocean/.env" 2>/dev/null \
  || [ -f "$HERE/.env" ] || { echo "No .env on the server and none here. Copy .env.example to .env and fill it in." >&2; exit 1; }

echo "==> copying backend"
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'dump.rdb' \
  --exclude 'celerybeat-schedule*' --exclude '.env' --exclude '.env.*' --exclude 'deploy/digitalocean/.env' \
  "$BACKEND/" "$TARGET:$REMOTE/app/"
if [ "$WITH_FRONTEND" = 1 ]; then
  echo "==> copying dashboard"
  rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
    --exclude 'node_modules' --exclude '.next' --exclude '.env*' --exclude '.git' --exclude 'coverage' \
    "$FRONTEND/" "$TARGET:$REMOTE/frontend/"
  scp "${SSH_OPTS[@]}" "$HERE/frontend.Dockerfile" "$TARGET:$REMOTE/frontend/Dockerfile.prod"
fi
if [ -f "$HERE/.env" ]; then
  echo "==> installing .env from this folder"
  scp "${SSH_OPTS[@]}" "$HERE/.env" "$TARGET:$REMOTE/app/deploy/digitalocean/.env"
  ssh "${SSH_OPTS[@]}" "$TARGET" "chmod 600 $REMOTE/app/deploy/digitalocean/.env"
fi

if [ "${BUILD_ONLY:-}" = 1 ]; then
  echo "==> building only (BUILD_ONLY=1); not starting anything"
  ssh "${SSH_OPTS[@]}" "$TARGET" "cd $REMOTE/app/deploy/digitalocean && docker compose --env-file .env $PROFILE -f docker-compose.prod.yml build"
  echo "Built. Start it with: ./deploy.sh $TARGET"
  exit 0
fi

echo "==> building and starting"
ssh "${SSH_OPTS[@]}" "$TARGET" "cd $REMOTE/app/deploy/digitalocean && chmod +x *.sh && \
  docker compose --env-file .env $PROFILE -f docker-compose.prod.yml up -d --build --remove-orphans"

echo "==> waiting for the API"
ssh "${SSH_OPTS[@]}" "$TARGET" "cd $REMOTE/app/deploy/digitalocean && for i in \$(seq 1 40); do \
  s=\$(docker compose -f docker-compose.prod.yml ps api --format '{{.Health}}'); [ \"\$s\" = healthy ] && break; sleep 5; done; \
  echo api: \$s; docker compose -f docker-compose.prod.yml ps"
echo "Done. Dashboard: https://\$(grep ^APP_DOMAIN= '$HERE/.env' 2>/dev/null | cut -d= -f2 || echo your-app-domain)"
