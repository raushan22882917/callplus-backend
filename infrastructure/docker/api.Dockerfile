FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

ENV PYTHONPATH=/app:/app/apps/api \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY packages ./packages
COPY apps ./apps

COPY infrastructure/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run as an unprivileged user; /app stays root-owned and read-only to it.
RUN useradd --system --uid 10001 --no-create-home appuser
ENV HOME=/tmp UV_CACHE_DIR=/tmp/uv-cache
USER appuser

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
