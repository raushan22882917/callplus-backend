#!/bin/sh
set -e

# If the platform (Railway's deploy.startCommand, or docker-compose's
# `command:`) passed args, run exactly that instead — this is how the worker
# service reuses this same image without also racing the api service to run
# migrations on every deploy.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Migrations run as the database owner. When MIGRATION_DATABASE_URL is set,
# the serving DATABASE_URL is a non-superuser role so row-level security applies
# (superusers ignore it). The role is created here, after the schema exists.
if [ -n "${MIGRATION_DATABASE_URL:-}" ]; then
  DATABASE_URL="$MIGRATION_DATABASE_URL" uv run --no-sync alembic upgrade head
  DATABASE_URL="$MIGRATION_DATABASE_URL" uv run --no-sync python -c "from packages.database.app_role import ensure_app_role; ensure_app_role()"
else
  uv run --no-sync alembic upgrade head
fi
exec uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir apps/api
