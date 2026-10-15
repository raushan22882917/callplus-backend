#!/bin/sh
set -e

# If the platform (Railway's deploy.startCommand, or docker-compose's
# `command:`) passed args, run exactly that instead — this is how the worker
# service reuses this same image without also racing the api service to run
# migrations on every deploy.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

uv run --no-sync alembic upgrade head
exec uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir apps/api
