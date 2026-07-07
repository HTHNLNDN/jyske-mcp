"""
Standalone cleanup for the NULL-transaction_id dedup bug.

BACKGROUND: transactions.transaction_id is TEXT UNIQUE, and
lib/storage.py's store_transaction() upserts via
ON CONFLICT(transaction_id) DO UPDATE. Before the fix in lib/storage.py,
any Enable Banking transaction lacking both `transaction_id` and
`entry_reference` was stored with a NULL transaction_id — and SQLite
treats every NULL as distinct from every other NULL, so ON CONFLICT never
fired for these rows. Every overlapping sync re-inserted them, duplicating
real spend.

store_transaction() now assigns these rows a deterministic
"synth:<sha256>" id (see lib.storage.synthetic_transaction_id) so future
syncs upsert correctly. This script is the one-time cleanup for rows that
already accumulated duplicates under the old behavior — it is NOT an
Alembic migration (no schema change) and only ever touches rows where
transaction_id IS NULL; rows with a real bank id are never read or written.

For each NULL-transaction_id row:
  1. Compute its synthetic id from json.loads(raw_data) — the SAME raw
     transaction dict store_transaction hashes, via the same
     lib.storage helpers, so ids computed here always agree with ids
     future syncs will compute. (Never hashed from the typed `amount`
     column — see synthetic_transaction_id()'s docstring for why that
     would silently reintroduce duplication.)
  2. Group rows by that synthetic id.
  3. Within each group of >1, keep the oldest row (min created_at, then
     min id as a tiebreak) and mark the rest for deletion.
  4. Every surviving previously-NULL row (whether it had duplicates or
     not) gets its transaction_id backfilled to the synthetic id — a
     survivor left NULL would not match on the next sync and would
     duplicate again.

COLLISION CAVEAT: content-hash dedup cannot distinguish two genuinely
different transactions that happen to share account_uid + date + amount +
currency + description and both lack a bank id — this script (like the
synthetic id itself) will merge them into one row. Some legitimate
same-day, same-value charges may merge. There is no clean fix without a
bank-assigned id.

Usage:
    python scripts/dedupe_transactions.py            # dry-run (default) — reports only, writes nothing
    python scripts/dedupe_transactions.py --apply    # actually deletes duplicates and backfills ids

Idempotent: after --apply, no row has transaction_id IS NULL anymore, so
re-running (in either mode) finds nothing to do.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import DB_FILE
from lib.storage import synthetic_transaction_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicate rows and backfill transaction_id "
             "(default is dry-run: report only, write nothing).",
    )
    args = parser.parse_args()

    if not DB_FILE.exists():
        print(f"No database found at {DB_FILE} — nothing to do.")
        return

    conn = sqlite3.connect(str(DB_FILE))
    rows = conn.execute(
        "SELECT id, account_uid, raw_data, created_at FROM transactions "
        "WHERE transaction_id IS NULL"
    ).fetchall()

    if not rows:
        print("No NULL-transaction_id rows found — nothing to do.")
        conn.close()
        return

    groups: dict[str, list[tuple[int, str, str, float]]] = {}
    for row_id, account_uid, raw_data, created_at in rows:
        tx = json.loads(raw_data)
        synth_id = synthetic_transaction_id(account_uid, tx)
        groups.setdefault(synth_id, []).append((row_id, account_uid, synth_id, created_at))

    to_delete: list[int] = []
    to_backfill: list[tuple[str, int]] = []  # (synth_id, row_id)
    duplicate_groups = 0

    for synth_id, group in groups.items():
        if len(group) > 1:
            duplicate_groups += 1
            # Keep the oldest row: min created_at, tiebreak min id.
            survivor = min(group, key=lambda g: (g[3], g[0]))
            for row_id, _account_uid, _synth_id, _created_at in group:
                if row_id == survivor[0]:
                    to_backfill.append((synth_id, row_id))
                else:
                    to_delete.append(row_id)
        else:
            row_id = group[0][0]
            to_backfill.append((synth_id, row_id))

    print(f"NULL-transaction_id rows examined: {len(rows)}")
    print(f"Duplicate groups found:            {duplicate_groups}")
    print(f"Rows that would be removed:        {len(to_delete)}")
    print(f"Rows that would be backfilled:      {len(to_backfill)}")
    print(
        "Note: some legitimate same-day same-value charges may merge — "
        "content-hash dedup cannot tell them apart from true duplicates "
        "when neither has a bank-assigned transaction id."
    )

    if not args.apply:
        print("\nDry-run only — no changes written. Re-run with --apply to write them.")
        conn.close()
        return

    try:
        conn.execute("BEGIN")
        if to_delete:
            conn.executemany(
                "DELETE FROM transactions WHERE id = ?",
                [(row_id,) for row_id in to_delete],
            )
        conn.executemany(
            "UPDATE transactions SET transaction_id = ? WHERE id = ?",
            [(synth_id, row_id) for synth_id, row_id in to_backfill],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"\nApplied: removed {len(to_delete)} rows, backfilled {len(to_backfill)} rows.")


if __name__ == "__main__":
    main()
