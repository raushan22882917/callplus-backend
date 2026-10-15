.PHONY: dev worker migrate revision downgrade seed test lint realtime-build realtime-test up down

export PYTHONPATH := .:apps/api

dev:
	uv run uvicorn app.main:app --reload --app-dir apps/api

worker:
	uv run celery -A app.worker.celery_app worker --loglevel=info --pool=solo

migrate:
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(m)"

downgrade:
	uv run alembic downgrade -1

test:
	uv run pytest

lint:
	uv run ruff check .

realtime-build:
	cd apps/realtime && go build ./...

realtime-test:
	cd apps/realtime && go test ./...

up:
	docker compose up -d --build

down:
	docker compose down
