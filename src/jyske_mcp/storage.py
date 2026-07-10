import hashlib, json, logging, os, sqlite3, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from jyske_mcp.kernel.categorizer import categorize
from jyske_mcp.kernel.config import CONFIG_DIR, DB_FILE, SESSION_FILE, ROOT_DIR

log = logging.getLogger("storage")

_SESSION_FILE = SESSION_FILE
_CACHE_DB = DB_FILE
_CACHE_TTL = 6 * 3600  # aligns with 4-calls/day rate limit

# The only currency get_budget_status()'s spent/percent/status figures are
# computed against — budgets are set in DKK and there's no exchange rate, so
# non-DKK spend is surfaced separately (other_currency_amounts) rather than
# blended in. See the currency de-blending fix this constant is part of.
PRIMARY_CURRENCY = "DKK"


class SessionExpiredError(Exception):
    pass


def _extract_txn_fields(tx: dict) -> dict:
    """Lift the same date/amount/currency/description parsing store_transaction
    has always used, so store_transaction and scripts/dedupe_transactions.py
    derive these fields IDENTICALLY from a raw Enable Banking transaction
    dict. Do not change this extraction without checking both call sites —
    a divergence here would make synthetic_transaction_id() disagree between
    a live sync and the cleanup script."""
    date = tx.get("booking_date") or tx.get("value_date", "")
    amt = tx.get("transaction_amount", {})
    amount = amt.get("amount")
    currency = amt.get("currency", "")
    description = (
        tx.get("creditor_name")
        or (tx.get("remittance_information") or [""])[0]
        or tx.get("debtor_name", "")
    )
    return {
        "date": date,
        "amount": amount,
        "currency": currency,
        "description": description,
    }


def synthetic_transaction_id(account_uid: str, tx: dict) -> str:
    """Deterministic stand-in transaction_id for transactions Enable Banking
    doesn't give a transaction_id/entry_reference for. transaction_id is
    TEXT UNIQUE, but SQLite treats every NULL as distinct from every other
    NULL, so a plain `None` id would re-INSERT (duplicate) the same
    transaction on every overlapping sync instead of hitting the
    ON CONFLICT(transaction_id) upsert. Hashing a stable subset of fields
    gives these rows a real, stable UNIQUE key instead.

    CRITICAL: this MUST be computed from the raw transaction dict (the same
    `tx`/raw_data JSON both store_transaction and the cleanup script have),
    NEVER from the transactions.amount DB column. That column is REAL, so
    e.g. an API amount of "12.50" round-trips through SQLite as the float
    12.5 — hashing str(12.5) in the cleanup script would produce a
    different id than hashing the original "12.50" string at sync time,
    silently breaking dedup for every row the cleanup script touches.

    mcc is deliberately excluded — Enable Banking doesn't reliably repeat it
    across pages for the same transaction.

    COLLISION CAVEAT: this collapses two genuinely distinct transactions
    that share account_uid + date + amount + currency + description AND
    both lack a bank id into a single row, silently dropping one of them.
    There's no clean fix without a bank-assigned id — adding a positional
    disambiguator (e.g. an occurrence counter) would make the id depend on
    fetch order, which breaks cross-sync determinism and reintroduces the
    exact duplication bug this fix is for. Accepted tradeoff.
    """
    fields = _extract_txn_fields(tx)
    raw = "|".join([
        account_uid,
        str(fields["date"]),
        str(fields["amount"]),
        str(fields["currency"]),
        str(fields["description"]),
    ])
    return "synth:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Storage:
    def get_session(self) -> dict:
        if not _SESSION_FILE.exists():
            raise SessionExpiredError(
                "Your bank connection needs renewal — open Settings › Bank "
                "connection and tap Reconnect."
            )
        data = json.loads(_SESSION_FILE.read_text())
        valid_until = datetime.fromisoformat(data["valid_until"])
        if datetime.now(timezone.utc) > valid_until:
            raise SessionExpiredError(
                f"Session expired on {valid_until.strftime('%Y-%m-%d')}. "
                "Your bank connection needs renewal — open Settings › Bank "
                "connection and tap Reconnect."
            )
        return data

    def read_session_unchecked(self) -> dict | None:
        """Like get_session() but never raises on a missing/expired session —
        used by consent status/reconciliation logic that must inspect an
        expired session too."""
        if not _SESSION_FILE.exists():
            return None
        return json.loads(_SESSION_FILE.read_text())

    def save_session(self, data: dict) -> None:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(data, indent=2))

    def cache_get(self, key: str) -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT data, cached_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < _CACHE_TTL:
            return json.loads(row[0])
        return None

    def cache_set(self, key: str, data: dict) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, data, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), time.time()),
        )
        conn.commit()
        conn.close()

    def merchant_get(self, raw_name: str, conn=None) -> dict | None:
        own = conn is None
        if own:
            conn = self._db()
        row = conn.execute(
            "SELECT category_top, category_mid, category_leaf, resolved_name, mcc, source "
            "FROM merchants WHERE raw_name = ?",
            (raw_name,),
        ).fetchone()
        if own:
            conn.close()
        if row is None:
            return None
        return {
            "category_top":  row[0],
            "category_mid":  row[1],
            "category_leaf": row[2],
            "resolved_name": row[3],
            "mcc":           row[4],
            "source":        row[5],
        }

    def merchant_set(
        self,
        raw_name: str,
        category_top: str,
        category_mid: str,
        category_leaf: str,
        resolved_name: str = "",
        mcc: str = "",
        source: str = "llm",
        conn=None,
    ) -> None:
        own = conn is None
        if own:
            conn = self._db()
        conn.execute(
            """INSERT OR REPLACE INTO merchants
               (raw_name, category_top, category_mid, category_leaf,
                resolved_name, mcc, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_name, category_top, category_mid, category_leaf,
             resolved_name, mcc, source, time.time()),
        )
        if own:
            conn.commit()
            conn.close()

    def recategorize_from_transaction(
        self, transaction_id: int, category_top: str, category_mid: str
    ) -> dict | None:
        """
        User-driven correction: reassign a merchant's category from one of
        its transactions, and rewrite every historical transaction row for
        that same merchant (matched on description == merchants.raw_name,
        the same key backfill_categories()/store_transaction() use) to match.
        All-time, not month-scoped — intentional, mirrors how merchant-level
        categorization already works everywhere else in this codebase.

        `transaction_id` is transactions.id (the local primary key), NOT
        transactions.transaction_id (the bank's own reference column).

        Returns None if transaction_id doesn't resolve to a real row.
        """
        conn = self._db()
        row = conn.execute(
            "SELECT description, category_top FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        raw_name, old_category_top = row

        # Upsert the merchant row without touching resolved_name/mcc/created_at
        # for an existing merchant — merchant_set()'s INSERT OR REPLACE would
        # wipe those fields, so we don't reuse it here.
        conn.execute(
            """INSERT INTO merchants
               (raw_name, category_top, category_mid, category_leaf,
                resolved_name, mcc, source, created_at)
               VALUES (?, ?, ?, '', '', '', 'user', ?)
               ON CONFLICT(raw_name) DO UPDATE SET
                 category_top=excluded.category_top,
                 category_mid=excluded.category_mid,
                 category_leaf='',
                 source='user'""",
            (raw_name, category_top, category_mid, time.time()),
        )

        cur = conn.execute(
            "UPDATE transactions SET category_top = ?, category_mid = ?, category_leaf = '' "
            "WHERE description = ?",
            (category_top, category_mid, raw_name),
        )
        transactions_updated = cur.rowcount

        conn.commit()
        conn.close()

        return {
            "raw_name": raw_name,
            "old_category_top": old_category_top,
            "transactions_updated": transactions_updated,
        }

    def get_profile(self, key: str) -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT value FROM user_profile WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return json.loads(row[0])

    def set_profile(self, key: str, value: dict) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        conn.commit()
        conn.close()

    def get_all_profile(self) -> dict:
        """Every user_profile row as {key: value}, raw and untruncated —
        unlike get_memory()'s LLM-formatted text summary, this is the full
        structured data for the /audit/data endpoint."""
        conn = self._db()
        rows = conn.execute("SELECT key, value FROM user_profile").fetchall()
        conn.close()
        return {key: json.loads(value) for key, value in rows}

    def add_session_summary(self, summary: str, agent_id: str = "finance") -> None:
        conn = self._db()
        conn.execute(
            "INSERT INTO session_summaries (summary, created_at, agent_id) VALUES (?, ?, ?)",
            (summary, time.time(), agent_id),
        )
        conn.execute(
            "DELETE FROM session_summaries WHERE agent_id = ? AND id NOT IN "
            "(SELECT id FROM session_summaries WHERE agent_id = ? ORDER BY id DESC LIMIT 10)",
            (agent_id, agent_id),
        )
        conn.commit()
        conn.close()

    def get_recent_summaries(self, n: int = 3, agent_id: str = "finance") -> list[str]:
        conn = self._db()
        rows = conn.execute(
            "SELECT summary FROM session_summaries WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
            (agent_id, n),
        ).fetchall()
        conn.close()
        return [row[0] for row in reversed(rows)]

    def get_all_summaries(self) -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT summary, created_at FROM session_summaries ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return [{"summary": row[0], "created_at": row[1]} for row in rows]

    def store_transaction(self, account_uid: str, tx: dict, conn=None) -> None:
        """Insert/upsert one transaction row plus its inline category.

        conn=None (default): opens, commits and closes its own connection —
        unchanged behavior for existing single-transaction callers.
        conn=<borrowed connection>: runs on the caller's connection and
        does NOT commit or close it — the caller (store_transactions_batch)
        owns the transaction boundary. This matters under WAL's single-writer
        rule: opening a 2nd connection while a write transaction is still
        open on the first would raise SQLITE_BUSY, so the inline categorize
        below is folded onto the SAME connection rather than opening a new
        one.
        """
        own = conn is None
        if own:
            conn = self._db()

        fields = _extract_txn_fields(tx)
        date = fields["date"]
        amount = fields["amount"]
        currency = fields["currency"]
        description = fields["description"]
        mcc = tx.get("mcc") or tx.get("merchant_category_code", "")
        tid = tx.get("transaction_id") or tx.get("entry_reference")
        if tid is None:
            # No bank-assigned id — synthesize a stable one so this row
            # upserts on re-sync instead of duplicating. See
            # synthetic_transaction_id()'s docstring for the collision
            # caveat and why it must hash the raw tx dict, not DB columns.
            tid = synthetic_transaction_id(account_uid, tx)
        direction = tx.get("credit_debit_indicator")
        conn.execute(
            """INSERT INTO transactions
               (account_uid, transaction_id, date, amount, currency,
                description, mcc, direction, raw_data, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(transaction_id) DO UPDATE SET
                 date=excluded.date, amount=excluded.amount,
                 currency=excluded.currency, description=excluded.description,
                 mcc=excluded.mcc, direction=excluded.direction, raw_data=excluded.raw_data""",
            (account_uid, tid, date, amount, currency,
             description, mcc, direction, json.dumps(tx), time.time()),
        )

        # Persist the resolved category onto the row so budget queries can read it
        # directly. categorize() checks the merchant cache then MCC lookup, and
        # returns None when only an LLM can classify — those rows are filled in
        # later by the sync's batch categorizer (which writes the merchants table).
        if tid is not None:
            cat = categorize(description, mcc, self, conn=conn)
            if cat is not None:
                conn.execute(
                    "UPDATE transactions "
                    "SET category_top = ?, category_mid = ?, category_leaf = ? "
                    "WHERE transaction_id = ?",
                    (cat["category_top"], cat["category_mid"], cat["category_leaf"], tid),
                )

        if own:
            conn.commit()
            conn.close()

    def store_transactions_batch(self, account_uid: str, txs: list[dict]) -> None:
        """Store every transaction for one account on a single connection
        with a single commit, instead of one open/commit/close cycle per
        row — collapses O(N) fsyncs to O(1) per account. See
        scripts/benchmark_sync_writes.py for the before/after."""
        conn = self._db()
        try:
            for tx in txs:
                self.store_transaction(account_uid, tx, conn=conn)
            conn.commit()
        finally:
            conn.close()

    def most_recent_transaction_date(self, account_uid: str) -> str | None:
        """Cheap MAX(date) cursor for incremental sync — replaces
        jyske_mcp/jobs/sync.py's old _most_recent_tx_date, which pulled every cached
        transaction row (raw_data included) for the account just to read
        rows[0]["booking_date"] off the newest one."""
        conn = self._db()
        row = conn.execute(
            "SELECT MAX(date) FROM transactions WHERE account_uid = ?",
            (account_uid,),
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else None

    def backfill_categories(self) -> int:
        """
        Fill category_top/mid/leaf from the merchants cache for any
        transaction rows that still lack a category — e.g. rows inserted
        before their merchant was categorized. Mirrors the one-time backfill
        UPDATE in migrations/versions/784418892304_add_math_aggregation_tools.py
        so future syncs stay reconciled the same way. Returns rows affected.
        """
        conn = self._db()
        cur = conn.execute("""
            UPDATE transactions
            SET category_top = (
                    SELECT m.category_top FROM merchants m
                    WHERE m.raw_name = transactions.description
                ),
                category_mid = (
                    SELECT m.category_mid FROM merchants m
                    WHERE m.raw_name = transactions.description
                ),
                category_leaf = (
                    SELECT m.category_leaf FROM merchants m
                    WHERE m.raw_name = transactions.description
                )
            WHERE category_top IS NULL
              AND EXISTS (
                    SELECT 1 FROM merchants m WHERE m.raw_name = transactions.description
                  )
        """)
        conn.commit()
        affected = cur.rowcount
        conn.close()
        return affected

    def store_balance(self, account_uid: str, data: dict) -> None:
        conn = self._db()
        conn.execute(
            "INSERT INTO balances (account_uid, data, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(account_uid) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
            (account_uid, json.dumps(data), time.time()),
        )
        conn.commit()
        conn.close()

    def balance_fetched_at(self, account_uid: str) -> float | None:
        conn = self._db()
        row = conn.execute(
            "SELECT fetched_at FROM balances WHERE account_uid = ?",
            (account_uid,),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def remap_account_uid(self, old_uid: str, new_uid: str) -> None:
        """Re-key cached transactions/balances from an old account uid to a
        new one. uids can change across re-authorization sessions even for
        the same physical account; called from jyske_mcp/consent.py reconciliation
        when identification_hash matches an account from the prior session."""
        if old_uid == new_uid:
            return
        conn = self._db()
        conn.execute(
            "UPDATE transactions SET account_uid = ? WHERE account_uid = ?",
            (new_uid, old_uid),
        )
        # balances.account_uid is a PRIMARY KEY — move the row rather than
        # UPDATE, so this doesn't conflict if new_uid already has a row.
        row = conn.execute(
            "SELECT data, fetched_at FROM balances WHERE account_uid = ?",
            (old_uid,),
        ).fetchone()
        if row is not None:
            conn.execute(
                "INSERT INTO balances (account_uid, data, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(account_uid) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                (new_uid, row[0], row[1]),
            )
            conn.execute("DELETE FROM balances WHERE account_uid = ?", (old_uid,))
        conn.commit()
        conn.close()

    def get_transactions_cached(self, account_uid: str, date_from: str, date_to: str) -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT raw_data FROM transactions "
            "WHERE account_uid = ? AND date >= ? AND date <= ? "
            "ORDER BY date DESC",
            (account_uid, date_from, date_to),
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    def get_all_transactions(self) -> list[dict]:
        """Every transaction row, compact typed columns only — raw_data is
        deliberately excluded (see /audit/data in app.py: that endpoint must
        never expose the raw Enable Banking payload). Transactions are
        account-global, not agent-scoped, so this returns everything."""
        conn = self._db()
        rows = conn.execute(
            "SELECT id, account_uid, transaction_id, date, amount, currency, "
            "description, mcc, category_top, category_mid, category_leaf, "
            "direction, created_at "
            "FROM transactions ORDER BY date DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "id":             r[0],
                "account_uid":    r[1],
                "transaction_id": r[2],
                "date":           r[3],
                "amount":         r[4],
                "currency":       r[5],
                "description":    r[6],
                "mcc":            r[7],
                "category_top":   r[8],
                "category_mid":   r[9],
                "category_leaf":  r[10],
                "direction":      r[11],
                "created_at":     r[12],
            }
            for r in rows
        ]

    def get_balances_cached(self, account_uid: str) -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT data FROM balances WHERE account_uid = ?",
            (account_uid,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return json.loads(row[0])

    def get_last_sync(self) -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT started_at, completed_at, accounts_synced, transactions_fetched, "
            "new_transactions, errors FROM syncs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "started_at":           row[0],
            "completed_at":         row[1],
            "accounts_synced":      row[2],
            "transactions_fetched": row[3],
            "new_transactions":     row[4],
            "errors":               row[5],
        }

    def record_sync(
        self,
        started_at: float,
        completed_at: float,
        accounts_synced: int,
        transactions_fetched: int,
        new_transactions: int,
        errors: str,
    ) -> None:
        conn = self._db()
        conn.execute(
            "INSERT INTO syncs "
            "(started_at, completed_at, accounts_synced, transactions_fetched, new_transactions, errors) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (started_at, completed_at, accounts_synced, transactions_fetched, new_transactions, errors),
        )
        conn.commit()
        conn.close()

    def set_budget(
        self,
        category_top: str,
        limit_amount: float,
        period: str,
        category_mid: str | None = None,
        agent_id: str = "finance",
    ) -> None:
        conn = self._db()
        if category_mid is None:
            conn.execute(
                "UPDATE budgets SET active = 0 "
                "WHERE category_top = ? AND category_mid IS NULL AND period = ? "
                "AND agent_id = ? AND active = 1",
                (category_top, period, agent_id),
            )
        else:
            conn.execute(
                "UPDATE budgets SET active = 0 "
                "WHERE category_top = ? AND category_mid = ? AND period = ? "
                "AND agent_id = ? AND active = 1",
                (category_top, category_mid, period, agent_id),
            )
        conn.execute(
            "INSERT INTO budgets (category_top, category_mid, limit_amount, period, active, created_at, agent_id) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (category_top, category_mid, limit_amount, period, time.time(), agent_id),
        )
        conn.commit()
        conn.close()

    def get_budgets(self, agent_id: str = "finance") -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, category_top, category_mid, limit_amount, period, created_at "
            "FROM budgets WHERE active = 1 AND agent_id = ? ORDER BY category_top, category_mid",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [
            {
                "id":           r[0],
                "category_top": r[1],
                "category_mid": r[2],
                "limit_amount": r[3],
                "period":       r[4],
                "created_at":   r[5],
            }
            for r in rows
        ]

    def sum_spending(
        self,
        date_from: str,
        date_to: str,
        category_top: str | None = None,
        account_uid: str | None = None,
        group_by: str = "category",
    ) -> list[dict]:
        """
        Sum debit spending (direction != 'CRDT') between two ISO dates,
        grouped by the requested key. Always grouped by currency too — today
        every row is DKK, but this stops a future non-DKK account from
        silently blending into a DKK total (no currency conversion here,
        just corruption avoidance).
        """
        group_cols = {
            "category": "category_top",
            "mid":      "category_mid",
            "month":    "substr(date, 1, 7)",
            "none":     None,
        }
        if group_by not in group_cols:
            raise ValueError(
                f"Invalid group_by: {group_by!r}. Must be one of {sorted(group_cols)}"
            )
        group_col = group_cols[group_by]
        select_key = group_col if group_col is not None else "NULL"

        query = (
            f"SELECT {select_key}, currency, SUM(amount), COUNT(*) "
            "FROM transactions WHERE direction != 'CRDT' AND date BETWEEN ? AND ?"
        )
        params: list = [date_from, date_to]
        if category_top:
            query += " AND category_top = ?"
            params.append(category_top)
        if account_uid:
            query += " AND account_uid = ?"
            params.append(account_uid)
        query += f" GROUP BY {select_key}, currency"

        conn = self._db()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [
            {
                "key":      row[0],
                "currency": row[1],
                "amount":   round(row[2] or 0.0, 2),
                "count":    row[3],
            }
            for row in rows
        ]

    def get_transactions_by_category(
        self,
        date_from: str,
        date_to: str,
        category_top: str,
        category_mid: str | None = None,
        uncategorized: bool = False,
        account_uid: str | None = None,
    ) -> list[dict]:
        """
        Compact transaction rows (never raw_data — see the no-raw-transaction-
        data rule) for a single category/mid, newest first. Filtering MUST
        mirror sum_spending()'s exactly (direction != 'CRDT', date BETWEEN,
        category_top =) so line items always reconcile with the aggregate
        totals shown above them in the UI.
        """
        query = (
            "SELECT id, date, amount, currency, description "
            "FROM transactions WHERE direction != 'CRDT' AND date BETWEEN ? AND ? "
            "AND category_top = ?"
        )
        params: list = [date_from, date_to, category_top]
        if uncategorized:
            query += " AND (category_mid IS NULL OR category_mid = '')"
        elif category_mid is not None:
            query += " AND category_mid = ?"
            params.append(category_mid)
        if account_uid:
            query += " AND account_uid = ?"
            params.append(account_uid)
        query += " ORDER BY date DESC"

        conn = self._db()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [
            {
                "id":          r[0],
                "date":        r[1],
                "amount":      r[2],
                "currency":    r[3],
                "description": r[4],
            }
            for r in rows
        ]

    def get_recurring_candidates(self, lookback_days: int = 180, min_count: int = 3) -> list[dict]:
        """
        Return candidate merchants for recurring-charge classification:
        every debit merchant/currency pair with >= min_count charges in the
        lookback window, with the raw chronological (date, amount) sequence
        — the classification logic in server.py needs the actual sequence
        to detect price-change runs, not just aggregate stats.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        conn = self._db()
        rows = conn.execute(
            "SELECT t.date, t.amount, t.currency, t.category_top, t.category_leaf, "
            "       COALESCE(NULLIF(m.resolved_name, ''), t.description) AS merchant "
            "FROM transactions t "
            "LEFT JOIN merchants m ON t.description = m.raw_name "
            "WHERE t.direction != 'CRDT' AND t.date >= ? "
            "ORDER BY merchant, t.currency, t.date ASC",
            (cutoff,),
        ).fetchall()
        conn.close()

        groups: dict[tuple[str, str], dict] = {}
        for date, amount, currency, category_top, category_leaf, merchant in rows:
            key = (merchant, currency)
            g = groups.setdefault(
                key, {"merchant": merchant, "currency": currency, "charges": [], "categories": []}
            )
            g["charges"].append((date, amount))
            g["categories"].append(category_leaf or category_top)

        return [g for g in groups.values() if len(g["charges"]) >= min_count]

    def get_recurring_statuses(self) -> dict[tuple[str, str], dict]:
        """Bulk-read all recorded cancellation-confirmation statuses, keyed
        (merchant, currency). One query — callers merge in Python rather
        than querying per-merchant."""
        conn = self._db()
        rows = conn.execute(
            "SELECT merchant, currency, status, confirmed_at FROM recurring_charge_status"
        ).fetchall()
        conn.close()
        return {
            (merchant, currency): {"status": status, "confirmed_at": confirmed_at}
            for merchant, currency, status, confirmed_at in rows
        }

    _RECURRING_STATUSES = {"active", "cancelled", "unknown"}

    def set_recurring_status(self, merchant: str, currency: str, status: str) -> None:
        if status not in self._RECURRING_STATUSES:
            raise ValueError(
                f"Invalid status: {status!r}. Must be one of {sorted(self._RECURRING_STATUSES)}"
            )
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO recurring_charge_status "
            "(merchant, currency, status, confirmed_at) VALUES (?, ?, ?, ?)",
            (merchant, currency, status, time.time()),
        )
        conn.commit()
        conn.close()

    def current_month_window(self) -> tuple[str, str]:
        """(month_start, today) as ISO dates -- the single source of truth for
        the 'this month' budget window, shared by get_budget_status and the
        breakdown/line-items endpoints so they can never drift."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        return month_start, today

    def get_budget_status(self, agent_id: str = "finance") -> list[dict]:
        month_start, today = self.current_month_window()

        # category_top/direction are now reliable columns (see the migration
        # that added `direction` and backfilled categories), so this can go
        # through the same aggregation path as get_spending/compare_spending
        # instead of a divergent raw_data-parsing loop.
        #
        # Budgets are set (and scored) in PRIMARY_CURRENCY only — there's no
        # exchange rate, so non-DKK spend must never be blended into `spent`.
        # Keep the fold per-currency and pick out PRIMARY_CURRENCY below;
        # anything else is surfaced separately as other_currency_amounts.
        spending_rows = self.sum_spending(month_start, today, group_by="category")
        spending: dict[str, dict[str, float]] = {}   # cat -> {currency: amount}
        for row in spending_rows:
            cat = row["key"] or "Other"
            spending.setdefault(cat, {})
            spending[cat][row["currency"]] = round(
                spending[cat].get(row["currency"], 0.0) + row["amount"], 2
            )

        conn = self._db()
        budget_rows = conn.execute(
            "SELECT category_top, category_mid, limit_amount, period "
            "FROM budgets WHERE active = 1 AND agent_id = ?",
            (agent_id,),
        ).fetchall()
        conn.close()

        result = []
        for cat_top, cat_mid, limit_amount, period in budget_rows:
            if cat_mid:
                # Mid-level budget: don't reuse the top-level aggregate above
                # (that would blend in every other sub-category under the
                # same top category). Query mid-level spend scoped to this
                # top category only, so a same-named mid under a different
                # top category can never leak in.
                mid_rows = self.sum_spending(
                    month_start, today, category_top=cat_top, group_by="mid"
                )
                by_ccy: dict[str, float] = {}
                for row in mid_rows:
                    if row["key"] == cat_mid:
                        by_ccy[row["currency"]] = round(
                            by_ccy.get(row["currency"], 0.0) + row["amount"], 2
                        )
            else:
                by_ccy = spending.get(cat_top, {})

            spent = round(by_ccy.get(PRIMARY_CURRENCY, 0.0), 2)
            others = {c: a for c, a in by_ccy.items() if c != PRIMARY_CURRENCY and a}

            percent = round((spent / limit_amount) * 100, 1) if limit_amount > 0 else 0.0
            if percent < 80:
                status = "on_track"
            elif percent <= 100:
                status = "warning"
            else:
                status = "over"
            entry = {
                "category":     cat_top,
                "category_mid": cat_mid,
                "spent":        spent,
                "limit":        limit_amount,
                "period":       period,
                "percent":      percent,
                "status":       status,
            }
            if others:
                entry["other_currency_amounts"] = others
            result.append(entry)
        return result

    # ── goals ────────────────────────────────────────────────────────────────

    def get_goals(self, agent_id: str = "finance") -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, name, target_amount, current_amount, purpose, deadline, created_at, updated_at "
            "FROM goals WHERE agent_id = ? AND active = 1 ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [
            {
                "id":             r[0],
                "name":           r[1],
                "target_amount":  r[2],
                "current_amount": r[3],
                "purpose":        r[4],
                "deadline":       r[5],
                "created_at":     r[6],
                "updated_at":     r[7],
            }
            for r in rows
        ]

    def set_goal(
        self,
        agent_id: str,
        name: str,
        target_amount: float,
        purpose: str,
        deadline: str,
    ) -> int:
        now = time.time()
        conn = self._db()
        cur = conn.execute(
            "INSERT INTO goals "
            "(agent_id, name, target_amount, current_amount, purpose, deadline, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?, 1, ?, ?)",
            (agent_id, name, target_amount, purpose, deadline, now, now),
        )
        goal_id = cur.lastrowid
        conn.commit()
        conn.close()
        return goal_id

    def update_goal_progress(self, goal_id: int, current_amount: float) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE goals SET current_amount = ?, updated_at = ? WHERE id = ?",
            (current_amount, time.time(), goal_id),
        )
        conn.commit()
        conn.close()

    def deactivate_goal(self, goal_id: int) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE goals SET active = 0, updated_at = ? WHERE id = ?",
            (time.time(), goal_id),
        )
        conn.commit()
        conn.close()

    # ── onboarding ───────────────────────────────────────────────────────────

    _ONBOARDING_FIELDS = (
        "income", "income_day", "fixed_costs", "savings_monthly",
        "savings_purpose", "savings_target", "savings_deadline", "budget_style",
    )

    def get_onboarding(self, agent_id: str = "finance") -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT stage, income, income_day, fixed_costs, savings_monthly, savings_purpose, "
            "savings_target, savings_deadline, budget_style, completed_at, updated_at "
            "FROM onboarding WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        fixed_costs = None
        if row[3]:
            try:
                fixed_costs = json.loads(row[3])
            except (TypeError, ValueError):
                fixed_costs = row[3]
        return {
            "stage":            row[0],
            "income":           row[1],
            "income_day":       row[2],
            "fixed_costs":      fixed_costs,
            "savings_monthly":  row[4],
            "savings_purpose":  row[5],
            "savings_target":   row[6],
            "savings_deadline": row[7],
            "budget_style":     row[8],
            "completed_at":     row[9],
            "updated_at":       row[10],
        }

    def set_onboarding_stage(self, agent_id: str, stage: str, **kwargs) -> None:
        invalid = set(kwargs) - set(self._ONBOARDING_FIELDS)
        if invalid:
            raise ValueError(f"Unknown onboarding field(s): {', '.join(sorted(invalid))}")
        if "fixed_costs" in kwargs and not isinstance(kwargs["fixed_costs"], str):
            kwargs["fixed_costs"] = json.dumps(kwargs["fixed_costs"], ensure_ascii=False)

        existing = self.get_onboarding(agent_id) or {"budget_style": "honest"}
        existing.update(kwargs)

        now = time.time()
        conn = self._db()
        conn.execute(
            "INSERT INTO onboarding "
            "(agent_id, stage, income, income_day, fixed_costs, savings_monthly, "
            " savings_purpose, savings_target, savings_deadline, budget_style, completed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "  stage=excluded.stage, income=excluded.income, income_day=excluded.income_day, "
            "  fixed_costs=excluded.fixed_costs, savings_monthly=excluded.savings_monthly, "
            "  savings_purpose=excluded.savings_purpose, savings_target=excluded.savings_target, "
            "  savings_deadline=excluded.savings_deadline, budget_style=excluded.budget_style, "
            "  updated_at=excluded.updated_at",
            (
                agent_id, stage,
                existing.get("income"), existing.get("income_day"), existing.get("fixed_costs"),
                existing.get("savings_monthly"), existing.get("savings_purpose"),
                existing.get("savings_target"), existing.get("savings_deadline"),
                existing.get("budget_style", "honest"), existing.get("completed_at"), now,
            ),
        )
        conn.commit()
        conn.close()

    def complete_onboarding(self, agent_id: str) -> None:
        now = time.time()
        conn = self._db()
        conn.execute(
            "UPDATE onboarding SET completed_at = ?, updated_at = ? WHERE agent_id = ?",
            (now, now, agent_id),
        )
        conn.commit()
        conn.close()

    def reset_onboarding(self, agent_id: str) -> None:
        conn = self._db()
        conn.execute("DELETE FROM onboarding WHERE agent_id = ?", (agent_id,))
        conn.commit()
        conn.close()

    # ── budget history ───────────────────────────────────────────────────────

    def record_budget_history(
        self,
        agent_id: str,
        category_top: str,
        period: str,
        limit_amount: float,
        actual_amount: float,
    ) -> None:
        variance = round(actual_amount - limit_amount, 2)
        conn = self._db()
        conn.execute(
            "INSERT INTO budget_history "
            "(agent_id, category_top, period, limit_amount, actual_amount, variance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, category_top, period, limit_amount, actual_amount, variance, time.time()),
        )
        conn.commit()
        conn.close()

    def get_budget_history(self, agent_id: str, category_top: str, n_periods: int = 3) -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT period, limit_amount, actual_amount, variance, created_at, "
            "       strftime('%Y-%m', datetime(created_at, 'unixepoch')) AS month "
            "FROM budget_history "
            "WHERE agent_id = ? AND category_top = ? "
            "ORDER BY created_at DESC",
            (agent_id, category_top),
        ).fetchall()
        conn.close()

        # Recorded nightly, so a single calendar month has many rows — keep
        # only the most recent (highest created_at) snapshot per month.
        seen_months: set[str] = set()
        result = []
        for period, limit_amount, actual_amount, variance, created_at, month in rows:
            if month in seen_months:
                continue
            seen_months.add(month)
            result.append({
                "month":         month,
                "period":        period,
                "limit_amount":  limit_amount,
                "actual_amount": actual_amount,
                "variance":      variance,
                "created_at":    created_at,
            })
            if len(result) >= n_periods:
                break
        return result

    def get_overspend_patterns(self, agent_id: str, consecutive_months: int = 3) -> list[dict]:
        conn = self._db()
        categories = conn.execute(
            "SELECT DISTINCT category_top FROM budget_history WHERE agent_id = ?",
            (agent_id,),
        ).fetchall()
        conn.close()

        patterns = []
        for (category_top,) in categories:
            history = self.get_budget_history(agent_id, category_top, n_periods=consecutive_months)
            if len(history) < consecutive_months:
                continue
            if all(h["variance"] > 0 for h in history):
                patterns.append({
                    "category_top":       category_top,
                    "consecutive_months": consecutive_months,
                    "months":             [h["month"] for h in history],
                    "avg_variance":       round(sum(h["variance"] for h in history) / len(history), 2),
                })
        return patterns

    # ── tips ─────────────────────────────────────────────────────────────────

    _TIP_FEEDBACK_STATUSES = {"evaluated", "accepted", "rejected"}
    _TIP_FEEDBACK_REASON_CODES = {
        "not_representative", "already_addressed", "not_actionable",
        "inaccurate", "not_relevant", "other",
    }

    def create_tip(
        self,
        tip_date: str,
        window_from: str,
        window_to: str,
        tip_text: str,
        subject_key: str | None,
        category_top: str | None,
        based_on: str | None,
        signals_json: str,
        model: str,
        prompt_version: str,
        agent_id: str = "finance",
    ) -> int:
        """INSERT a new tip row, returning its id. UNIQUE(agent_id, tip_date)
        is the DB-level backstop against duplicates — the caller (jyske_mcp/jobs/tips.py)
        already checks get_tip_for_date first, but this raises
        sqlite3.IntegrityError instead of silently duplicating if that guard
        is ever bypassed or racing."""
        conn = self._db()
        cur = conn.execute(
            "INSERT INTO tips "
            "(agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            " subject_key, category_top, based_on, signals_json, model, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id, time.time(), tip_date, window_from, window_to, tip_text,
                subject_key, category_top, based_on, signals_json, model, prompt_version,
            ),
        )
        tip_id = cur.lastrowid
        conn.commit()
        conn.close()
        return tip_id

    def get_tip_for_date(self, tip_date: str, agent_id: str = "finance") -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT id, agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            "subject_key, category_top, based_on, signals_json, model, prompt_version, "
            "feedback_status, feedback_reason_code, feedback_reason_text, feedback_source, feedback_at "
            "FROM tips WHERE agent_id = ? AND tip_date = ?",
            (agent_id, tip_date),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return self._tip_row_to_dict(row)

    def get_recent_tips_with_feedback(self, n: int = 10, agent_id: str = "finance") -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            "subject_key, category_top, based_on, signals_json, model, prompt_version, "
            "feedback_status, feedback_reason_code, feedback_reason_text, feedback_source, feedback_at "
            "FROM tips WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, n),
        ).fetchall()
        conn.close()
        return [self._tip_row_to_dict(row) for row in rows]

    def get_rejected_subject_keys(self, since_days: int = 30, agent_id: str = "finance") -> set[str]:
        cutoff = time.time() - since_days * 86400
        conn = self._db()
        rows = conn.execute(
            "SELECT DISTINCT subject_key FROM tips "
            "WHERE agent_id = ? AND feedback_status = 'rejected' "
            "AND subject_key IS NOT NULL AND created_at >= ?",
            (agent_id, cutoff),
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}

    def set_tip_feedback(
        self,
        tip_id: int,
        feedback_status: str,
        reason_code: str | None,
        reason_text: str | None,
        source: str,
    ) -> None:
        if feedback_status not in self._TIP_FEEDBACK_STATUSES:
            raise ValueError(
                f"Invalid feedback_status: {feedback_status!r}. "
                f"Must be one of {sorted(self._TIP_FEEDBACK_STATUSES)}"
            )
        if reason_code is not None and reason_code not in self._TIP_FEEDBACK_REASON_CODES:
            raise ValueError(
                f"Invalid reason_code: {reason_code!r}. "
                f"Must be one of {sorted(self._TIP_FEEDBACK_REASON_CODES)}"
            )
        conn = self._db()
        conn.execute(
            "UPDATE tips SET feedback_status = ?, feedback_reason_code = ?, "
            "feedback_reason_text = ?, feedback_source = ?, feedback_at = ? "
            "WHERE id = ?",
            (feedback_status, reason_code, reason_text, source, time.time(), tip_id),
        )
        conn.commit()
        conn.close()

    def get_labeled_tips(self, agent_id: str = "finance") -> list[dict]:
        """Eval-set export query: every tip with feedback recorded, oldest
        first. No caller needs this yet — it exists so accumulated tips +
        feedback can be exported as an evaluation dataset later."""
        conn = self._db()
        rows = conn.execute(
            "SELECT tip_text, signals_json, based_on, window_from, window_to, model, "
            "prompt_version, feedback_status, feedback_reason_code, feedback_reason_text "
            "FROM tips WHERE agent_id = ? AND feedback_status != 'pending' ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [
            {
                "tip_text":             r[0],
                "signals_json":         r[1],
                "based_on":             r[2],
                "window_from":          r[3],
                "window_to":            r[4],
                "model":                r[5],
                "prompt_version":       r[6],
                "feedback_status":      r[7],
                "feedback_reason_code": r[8],
                "feedback_reason_text": r[9],
            }
            for r in rows
        ]

    def get_all_tips_with_feedback(self, agent_id: str = "finance") -> list[dict]:
        """Every tip row for the agent, any feedback_status (including
        pending), full columns — unlike get_labeled_tips() (feedback-only
        subset of columns, excludes pending) or get_recent_tips_with_feedback()
        (capped at n). Used by /audit/data for a complete, unpaginated dump."""
        conn = self._db()
        rows = conn.execute(
            "SELECT id, agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            "subject_key, category_top, based_on, signals_json, model, prompt_version, "
            "feedback_status, feedback_reason_code, feedback_reason_text, feedback_source, feedback_at "
            "FROM tips WHERE agent_id = ? ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [self._tip_row_to_dict(row) for row in rows]

    @staticmethod
    def _tip_row_to_dict(row) -> dict:
        return {
            "id":                   row[0],
            "agent_id":             row[1],
            "created_at":           row[2],
            "tip_date":             row[3],
            "window_from":          row[4],
            "window_to":            row[5],
            "tip_text":             row[6],
            "subject_key":          row[7],
            "category_top":         row[8],
            "based_on":             row[9],
            "signals_json":         row[10],
            "model":                row[11],
            "prompt_version":       row[12],
            "feedback_status":      row[13],
            "feedback_reason_code": row[14],
            "feedback_reason_text": row[15],
            "feedback_source":      row[16],
            "feedback_at":          row[17],
        }

    # ── provider keys ────────────────────────────────────────────────────────

    def get_provider_key(self, provider: str) -> str | None:
        conn = self._db()
        row = conn.execute(
            "SELECT api_key FROM provider_keys WHERE provider = ?", (provider,)
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def set_provider_key(self, provider: str, api_key: str) -> None:
        now = time.time()
        conn = self._db()
        conn.execute(
            "INSERT INTO provider_keys (provider, api_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET api_key=excluded.api_key, updated_at=excluded.updated_at",
            (provider, api_key, now, now),
        )
        conn.commit()
        conn.close()

    def delete_provider_key(self, provider: str) -> None:
        conn = self._db()
        conn.execute("DELETE FROM provider_keys WHERE provider = ?", (provider,))
        conn.commit()
        conn.close()

    def list_providers_with_keys(self) -> set[str]:
        conn = self._db()
        rows = conn.execute("SELECT provider FROM provider_keys").fetchall()
        conn.close()
        return {row[0] for row in rows}

    # ── agents ───────────────────────────────────────────────────────────────

    def get_agents(self) -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, name, description, model, created_at, updated_at FROM agents ORDER BY id"
        ).fetchall()
        conn.close()
        return [
            {
                "id":          r[0],
                "name":        r[1],
                "description": r[2],
                "model":       r[3],
                "created_at":  r[4],
                "updated_at":  r[5],
            }
            for r in rows
        ]

    def get_agent(self, agent_id: str) -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT id, name, description, model, created_at, updated_at FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "id":          row[0],
            "name":        row[1],
            "description": row[2],
            "model":       row[3],
            "created_at":  row[4],
            "updated_at":  row[5],
        }

    def set_agent_model(self, agent_id: str, model: str) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE agents SET model = ?, updated_at = ? WHERE id = ?",
            (model, time.time(), agent_id),
        )
        conn.commit()
        conn.close()

    def _db(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CONFIG_DIR, 0o700)
        conn = sqlite3.connect(str(_CACHE_DB))
        # Unconditional, not just on first creation — a prior run could have
        # left cache.db with looser permissions (e.g. restored from a backup,
        # or created before this hardening existed), so re-assert 0600 on
        # every connection rather than gating on is_new.
        os.chmod(_CACHE_DB, 0o600)
        return conn


def _check_schema_version() -> None:
    """Warn once at startup if the on-disk schema isn't at the latest Alembic
    revision. Storage no longer creates tables itself — see migrations/."""
    if not _CACHE_DB.exists():
        log.warning("Database schema is not up to date — run: alembic upgrade head")
        return

    conn = sqlite3.connect(str(_CACHE_DB))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()

    if row is None:
        log.warning("Database schema is not up to date — run: alembic upgrade head")
        return

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(ROOT_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT_DIR / "migrations"))
        head_rev = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return  # don't block startup if alembic itself can't be introspected

    if row[0] != head_rev:
        log.warning("Database schema is not up to date — run: alembic upgrade head")


_check_schema_version()
