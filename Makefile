.PHONY: install infra migrate test check api worker worker-reports dispatcher scheduler collector catalog seed
install:
	uv sync --locked
infra:
	docker compose up -d --wait
migrate:
	uv run alembic upgrade head
test:
	PA_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:local-owner-only@localhost:55432/portfolio_test uv run alembic upgrade head
	uv run pytest -q
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run alembic check
api:
	uv run portfolio-api
worker:
	uv run celery -A portfolio_backend.worker:celery worker -Q interactive_ai,privacy --concurrency=2 --loglevel=INFO
worker-reports:
	uv run celery -A portfolio_backend.worker:celery worker -Q reports,daily_reports,weekly_reports --concurrency=2 --loglevel=INFO
dispatcher:
	uv run portfolio-dispatcher
scheduler:
	uv run portfolio-scheduler
collector:
	uv run --extra market-data portfolio-data
catalog:
	uv run --extra market-data portfolio-catalog
seed:
	uv run portfolio-seed
