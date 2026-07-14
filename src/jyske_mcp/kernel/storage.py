import hashlib, json, logging, os, sqlite3, time
from datetime import datetime, timezone
from typing import Any

from jyske_mcp.kernel.categorizer import categorize
from jyske_mcp.kernel.config import CONFIG_DIR, DB_FILE, SESSION_FILE, ROOT_DIR
from jyske_mcp.kernel.dto import (
    AgentDTO,
    MerchantCategoryDTO,
    SummaryDTO,
    SyncRecordDTO,
    TransactionRowDTO,
)

log = logging.getLogger("storage")

_SESSION_FILE = SESSION_FILE
_CACHE_DB = DB_FILE
_CACHE_TTL = 6 * 3600  # aligns with 4-calls/day rate limit


class SessionExpiredError(Exception):
    pass


def _extract_txn_fields(tx: dict[str, Any]) -> dict[str, Any]:
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


def synthetic_transaction_id(account_uid: str, tx: dict[str, Any]) -> str:
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


class KernelStorage:
    """Generic-primitive storage surface: session, cache, merchant
    categorization, transactions/balances, syncs, provider_keys, agents,
    session_summaries, user_profile. No finance semantics (no
    `direction != 'CRDT'` money math, no budget/goal/tip/recurring/
    onboarding concepts) — see
    .agent/epics/vsa-restructure-blueprint.md §2 for the exact bucketing.

    Kernel-only: this module may import jyske_mcp.kernel.* alone, never
    jyske_mcp.slices/jyske_mcp.platform (see pyproject.toml's "Kernel
    imports nothing upward" import-linter contract). Physically relocated
    here from jyske_mcp/storage.py at deliverable #6;
    jyske_mcp/slices/finance/storage.py's FinanceStorage extends this class
    for the finance-domain queries.
    """

    def _db(self) -> sqlite3.Connection:
        """The single connection primitive both KernelStorage and
        FinanceStorage use (FinanceStorage extends this class rather than
        opening its own sqlite3.connect — see FinanceStorage's docstring).
        Re-reads _CACHE_DB/CONFIG_DIR at call time, which is what lets
        tests/conftest.py's full_schema_storage fixture (and every other
        fixture in this suite) redirect every Storage()/KernelStorage()/
        FinanceStorage() instance by monkeypatching those two module
        globals — on THIS module (jyske_mcp.kernel.storage), since that is
        where this function object's __globals__ point regardless of which
        subclass instance calls it."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CONFIG_DIR, 0o700)
        conn = sqlite3.connect(str(_CACHE_DB))
        # Unconditional, not just on first creation — a prior run could have
        # left cache.db with looser permissions (e.g. restored from a backup,
        # or created before this hardening existed), so re-assert 0600 on
        # every connection rather than gating on is_new.
        os.chmod(_CACHE_DB, 0o600)
        return conn

    # ── session ──────────────────────────────────────────────────────────────
    # Deliberately dict, not SessionDTO: get_session/read_session_unchecked/
    # save_session round-trip the raw Enable Banking session payload
    # byte-for-byte (tests/test_consent_flow.py pins exact equality on the
    # saved `accounts` list) — see jyske_mcp/kernel/dto.py's module docstring.
    # Callers wanting typed access build SessionDTO/AccountDTO via
    # .from_raw() themselves (see mcp/server.py's list_accounts/get_balances).

    def get_session(self) -> dict[str, Any]:
        if not _SESSION_FILE.exists():
            raise SessionExpiredError(
                "Your bank connection needs renewal — open Settings › Bank "
                "connection and tap Reconnect."
            )
        data: dict[str, Any] = json.loads(_SESSION_FILE.read_text())
        valid_until = datetime.fromisoformat(data["valid_until"])
        if datetime.now(timezone.utc) > valid_until:
            raise SessionExpiredError(
                f"Session expired on {valid_until.strftime('%Y-%m-%d')}. "
                "Your bank connection needs renewal — open Settings › Bank "
                "connection and tap Reconnect."
            )
        return data

    def read_session_unchecked(self) -> dict[str, Any] | None:
        """Like get_session() but never raises on a missing/expired session —
        used by consent status/reconciliation logic that must inspect an
        expired session too."""
        if not _SESSION_FILE.exists():
            return None
        data: dict[str, Any] = json.loads(_SESSION_FILE.read_text())
        return data

    def save_session(self, data: dict[str, Any]) -> None:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(data, indent=2))

    # ── cache (opaque k/v blobs — never DTO'd) ──────────────────────────────

    def cache_get(self, key: str) -> dict[str, Any] | None:
        conn = self._db()
        row = conn.execute(
            "SELECT data, cached_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < _CACHE_TTL:
            result: dict[str, Any] = json.loads(row[0])
            return result
        return None

    def cache_set(self, key: str, data: dict[str, Any]) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, data, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), time.time()),
        )
        conn.commit()
        conn.close()

    # ── merchant categorization ─────────────────────────────────────────────

    def merchant_get(
        self, raw_name: str, conn: sqlite3.Connection | None = None
    ) -> MerchantCategoryDTO | None:
        own = conn is None
        if own:
            conn = self._db()
        assert conn is not None
        row = conn.execute(
            "SELECT category_top, category_mid, category_leaf, resolved_name, mcc, source "
            "FROM merchants WHERE raw_name = ?",
            (raw_name,),
        ).fetchone()
        if own:
            conn.close()
        if row is None:
            return None
        return MerchantCategoryDTO(
            category_top=row[0],
            category_mid=row[1],
            category_leaf=row[2],
            resolved_name=row[3],
            mcc=row[4],
            source=row[5],
        )

    def merchant_set(
        self,
        raw_name: str,
        category_top: str,
        category_mid: str | None,
        category_leaf: str,
        resolved_name: str = "",
        mcc: str = "",
        source: str = "llm",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        own = conn is None
        if own:
            conn = self._db()
        assert conn is not None
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
        self, transaction_id: int, category_top: str, category_mid: str | None
    ) -> dict[str, Any] | None:
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

        Small, ad-hoc result shape (not one of the §3 DTOs) — kept as a
        plain dict, unchanged.
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

    # ── user_profile (opaque JSON — never DTO'd) ────────────────────────────

    def get_profile(self, key: str) -> dict[str, Any] | None:
        conn = self._db()
        row = conn.execute(
            "SELECT value FROM user_profile WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row[0])
        return result

    def set_profile(self, key: str, value: dict[str, Any]) -> None:
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO user_profile (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        conn.commit()
        conn.close()

    def get_all_profile(self) -> dict[str, Any]:
        """Every user_profile row as {key: value}, raw and untruncated —
        unlike get_memory()'s LLM-formatted text summary, this is the full
        structured data for the /audit/data endpoint."""
        conn = self._db()
        rows = conn.execute("SELECT key, value FROM user_profile").fetchall()
        conn.close()
        return {key: json.loads(value) for key, value in rows}

    # ── session_summaries ────────────────────────────────────────────────────

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

    def get_all_summaries(self) -> list[SummaryDTO]:
        conn = self._db()
        rows = conn.execute(
            "SELECT summary, created_at FROM session_summaries ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return [SummaryDTO(summary=row[0], created_at=row[1]) for row in rows]

    # ── transactions ─────────────────────────────────────────────────────────

    def store_transaction(
        self, account_uid: str, tx: dict[str, Any], conn: sqlite3.Connection | None = None
    ) -> None:
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
        assert conn is not None

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
        # directly. categorize() checks the merchant cache and returns None when
        # only an LLM can classify — those rows are filled in later by the
        # sync's batch categorizer (which writes the merchants table).
        if tid is not None:
            cat = categorize(description, mcc, self, conn=conn)
            if cat is not None:
                conn.execute(
                    "UPDATE transactions "
                    "SET category_top = ?, category_mid = ?, category_leaf = ? "
                    "WHERE transaction_id = ?",
                    (cat.category_top, cat.category_mid, cat.category_leaf, tid),
                )

        if own:
            conn.commit()
            conn.close()

    def store_transactions_batch(self, account_uid: str, txs: list[dict[str, Any]]) -> None:
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

    def get_transactions_cached(
        self, account_uid: str, date_from: str, date_to: str
    ) -> list[dict[str, Any]]:
        """Raw Enable Banking transaction dicts — deliberately opaque, NOT
        DTO'd (see jyske_mcp/kernel/dto.py's module docstring)."""
        conn = self._db()
        rows = conn.execute(
            "SELECT raw_data FROM transactions "
            "WHERE account_uid = ? AND date >= ? AND date <= ? "
            "ORDER BY date DESC",
            (account_uid, date_from, date_to),
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    def get_all_transactions(self) -> list[TransactionRowDTO]:
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
            TransactionRowDTO(
                id=r[0],
                account_uid=r[1],
                transaction_id=r[2],
                date=r[3],
                amount=r[4],
                currency=r[5],
                description=r[6],
                mcc=r[7],
                category_top=r[8],
                category_mid=r[9],
                category_leaf=r[10],
                direction=r[11],
                created_at=r[12],
            )
            for r in rows
        ]

    # ── balances ─────────────────────────────────────────────────────────────
    # Deliberately dict, not BalanceSnapshotDTO: store_balance/
    # get_balances_cached round-trip the raw Enable Banking balances payload
    # byte-for-byte (tests/test_consent_flow.py pins exact equality after
    # remap_account_uid) — see jyske_mcp/kernel/dto.py's module docstring.
    # Callers wanting typed access build BalanceSnapshotDTO/BalanceLineDTO via
    # .from_raw() themselves (see mcp/server.py's get_balances).

    def store_balance(self, account_uid: str, data: dict[str, Any]) -> None:
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

    def get_balances_cached(self, account_uid: str) -> dict[str, Any] | None:
        conn = self._db()
        row = conn.execute(
            "SELECT data FROM balances WHERE account_uid = ?",
            (account_uid,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row[0])
        return result

    # ── syncs ────────────────────────────────────────────────────────────────

    def get_last_sync(self) -> dict[str, Any] | None:
        """Returns plain dict (not SyncRecordDTO) — jyske_mcp.kernel.sync.
        is_sync_stale(last_sync: dict | None, ...) is a pure function tested
        directly with hand-built partial dicts (tests/jobs/test_sync_freshness.py)
        and does `last_sync["completed_at"]` subscript access, so the value
        flowing into it must stay dict-shaped. SyncRecordDTO is still built
        here for validation/typing, then converted back via model_dump()."""
        conn = self._db()
        row = conn.execute(
            "SELECT started_at, completed_at, accounts_synced, transactions_fetched, "
            "new_transactions, errors FROM syncs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return None
        dto = SyncRecordDTO(
            started_at=row[0],
            completed_at=row[1],
            accounts_synced=row[2],
            transactions_fetched=row[3],
            new_transactions=row[4],
            errors=row[5],
        )
        return dto.model_dump()

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

    def get_agents(self) -> list[AgentDTO]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, name, description, model, created_at, updated_at FROM agents ORDER BY id"
        ).fetchall()
        conn.close()
        return [
            AgentDTO(id=r[0], name=r[1], description=r[2], model=r[3], created_at=r[4], updated_at=r[5])
            for r in rows
        ]

    def get_agent(self, agent_id: str) -> AgentDTO | None:
        conn = self._db()
        row = conn.execute(
            "SELECT id, name, description, model, created_at, updated_at FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return AgentDTO(
            id=row[0], name=row[1], description=row[2], model=row[3], created_at=row[4], updated_at=row[5]
        )

    def set_agent_model(self, agent_id: str, model: str) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE agents SET model = ?, updated_at = ? WHERE id = ?",
            (model, time.time(), agent_id),
        )
        conn.commit()
        conn.close()


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
