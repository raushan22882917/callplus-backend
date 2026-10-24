#!/usr/bin/env bash
# Deploys (or updates) the stack on the droplet from your computer:
#   ./deploy.sh root@203.0.113.10 [/path/to/call-center-frontend]
# Copies the backend and the dashboard to the server, builds the images there, starts the stack
# and waits for the API to be healthy. Re-run it for every update; data is kept.
set -euo pipefail
TARGET="${1:?usage: ./deploy.sh user@host [frontend-dir]}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(cd "$HERE/../.." && pwd)"
FRONTEND="${2:-$BACKEND/../call-center-frontend}"
REMOTE=/opt/callpulse
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)

[ -d "$FRONTEND" ] || { echo "frontend folder not found: $FRONTEND" >&2; exit 1; }
ssh "${SSH_OPTS[@]}" "$TARGET" "test -f $REMOTE/app/deploy/digitalocean/.env" 2>/dev/null \
  || [ -f "$HERE/.env" ] || { echo "No .env on the server and none here. Copy .env.example to .env and fill it in." >&2; exit 1; }

echo "==> copying backend"
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'dump.rdb' \
  --exclude 'celerybeat-schedule*' --exclude '.env' --exclude '.env.*' --exclude 'deploy/digitalocean/.env' \
  "$BACKEND/" "$TARGET:$REMOTE/app/"
echo "==> copying dashboard"
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude 'node_modules' --exclude '.next' --exclude '.env*' --exclude '.git' --exclude 'coverage' \
  "$FRONTEND/" "$TARGET:$REMOTE/frontend/"
scp "${SSH_OPTS[@]}" "$HERE/frontend.Dockerfile" "$TARGET:$REMOTE/frontend/Dockerfile.prod"
if [ -f "$HERE/.env" ]; then
  echo "==> installing .env from this folder"
  scp "${SSH_OPTS[@]}" "$HERE/.env" "$TARGET:$REMOTE/app/deploy/digitalocean/.env"
  ssh "${SSH_OPTS[@]}" "$TARGET" "chmod 600 $REMOTE/app/deploy/digitalocean/.env"
fi

echo "==> building and starting"
ssh "${SSH_OPTS[@]}" "$TARGET" "cd $REMOTE/app/deploy/digitalocean && chmod +x *.sh && \
  docker compose --env-file .env -f docker-compose.prod.yml up -d --build --remove-orphans"

echo "==> waiting for the API"
ssh "${SSH_OPTS[@]}" "$TARGET" "cd $REMOTE/app/deploy/digitalocean && for i in \$(seq 1 40); do \
  s=\$(docker compose -f docker-compose.prod.yml ps api --format '{{.Health}}'); [ \"\$s\" = healthy ] && break; sleep 5; done; \
  echo api: \$s; docker compose -f docker-compose.prod.yml ps"
echo "Done. Dashboard: https://\$(grep ^APP_DOMAIN= '$HERE/.env' 2>/dev/null | cut -d= -f2 || echo your-app-domain)"
