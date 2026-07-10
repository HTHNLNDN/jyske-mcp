.PHONY: install dev build start sync migrate migration db-status db-history langfuse langfuse-stop test lint dedupe bench

# One-time (and after pulling dependency changes): editable install of the
# src/ package + runtime deps, into the active venv.
install:
	pip install -e . && pip install -r requirements.txt

dev:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

start:
	python -m jyske_mcp.web.app

sync:
	python -m jyske_mcp.jobs.scheduler

migrate:
	alembic upgrade head

migration:
	alembic revision -m "$(name)"

db-status:
	alembic current

db-history:
	alembic history

langfuse:
	docker compose -f docker/langfuse/docker-compose.yml up -d

langfuse-stop:
	docker compose -f docker/langfuse/docker-compose.yml down

test:
	pytest

# Enforces the kernel/slices/platform import boundaries declared in
# pyproject.toml's [tool.importlinter] contracts. Requires import-linter,
# installed via requirements-dev.txt.
lint:
	lint-imports

# Dry-run report of the NULL-transaction_id dedup cleanup (writes nothing).
# Pass --apply directly to scripts/dedupe_transactions.py to actually
# delete duplicates and backfill transaction_id — not wired up here on
# purpose, so this target can never accidentally write to the real DB.
dedupe:
	python scripts/dedupe_transactions.py

# Benchmarks the per-account batched-insert fix (Storage.store_transactions_batch)
# against the old per-row store_transaction loop. Standalone, writes only to a
# throwaway temp DB — never touches the real cache.db.
bench:
	python scripts/benchmark_sync_writes.py
