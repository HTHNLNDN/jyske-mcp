.PHONY: dev build start sync migrate migration db-status db-history langfuse langfuse-stop test dedupe bench

dev:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

start:
	python app.py

sync:
	python cron/scheduler.py

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
