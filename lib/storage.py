import json, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path

_SESSION_FILE = Path("~/.config/mcp-bank/session.json").expanduser()
_CACHE_DB = Path("~/.config/mcp-bank/cache.db").expanduser()
_CACHE_TTL = 6 * 3600  # aligns with 4-calls/day rate limit


class SessionExpiredError(Exception):
    pass


class Storage:
    def get_session(self) -> dict:
        if not _SESSION_FILE.exists():
            raise SessionExpiredError(
                "No session found. Run setup_consent.py to authenticate."
            )
        data = json.loads(_SESSION_FILE.read_text())
        valid_until = datetime.fromisoformat(data["valid_until"])
        if datetime.now(timezone.utc) > valid_until:
            raise SessionExpiredError(
                f"Session expired on {valid_until.strftime('%Y-%m-%d')}. "
                "Run setup_consent.py to re-authenticate."
            )
        return data

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

    def merchant_get(self, raw_name: str) -> dict | None:
        conn = self._db()
        row = conn.execute(
            "SELECT category_top, category_mid, category_leaf, resolved_name, mcc, source "
            "FROM merchants WHERE raw_name = ?",
            (raw_name,),
        ).fetchone()
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
    ) -> None:
        conn = self._db()
        conn.execute(
            """INSERT OR REPLACE INTO merchants
               (raw_name, category_top, category_mid, category_leaf,
                resolved_name, mcc, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_name, category_top, category_mid, category_leaf,
             resolved_name, mcc, source, time.time()),
        )
        conn.commit()
        conn.close()

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

    def add_session_summary(self, summary: str) -> None:
        conn = self._db()
        conn.execute(
            "INSERT INTO session_summaries (summary, created_at) VALUES (?, ?)",
            (summary, time.time()),
        )
        conn.execute(
            "DELETE FROM session_summaries WHERE id NOT IN "
            "(SELECT id FROM session_summaries ORDER BY id DESC LIMIT 10)"
        )
        conn.commit()
        conn.close()

    def get_recent_summaries(self, n: int = 3) -> list[str]:
        conn = self._db()
        rows = conn.execute(
            "SELECT summary FROM session_summaries ORDER BY id DESC LIMIT ?", (n,)
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

    def store_transaction(self, account_uid: str, tx: dict) -> None:
        date = tx.get("booking_date") or tx.get("value_date", "")
        amt = tx.get("transaction_amount", {})
        amount = amt.get("amount")
        currency = amt.get("currency", "")
        description = (
            tx.get("creditor_name")
            or (tx.get("remittance_information") or [""])[0]
            or tx.get("debtor_name", "")
        )
        mcc = tx.get("mcc") or tx.get("merchant_category_code", "")
        tid = tx.get("transaction_id") or tx.get("entry_reference")
        conn = self._db()
        conn.execute(
            """INSERT INTO transactions
               (account_uid, transaction_id, date, amount, currency,
                description, mcc, raw_data, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(transaction_id) DO UPDATE SET
                 date=excluded.date, amount=excluded.amount,
                 currency=excluded.currency, description=excluded.description,
                 mcc=excluded.mcc, raw_data=excluded.raw_data""",
            (account_uid, tid, date, amount, currency,
             description, mcc, json.dumps(tx), time.time()),
        )
        conn.commit()
        conn.close()

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
    ) -> None:
        conn = self._db()
        if category_mid is None:
            conn.execute(
                "UPDATE budgets SET active = 0 "
                "WHERE category_top = ? AND category_mid IS NULL AND period = ? AND active = 1",
                (category_top, period),
            )
        else:
            conn.execute(
                "UPDATE budgets SET active = 0 "
                "WHERE category_top = ? AND category_mid = ? AND period = ? AND active = 1",
                (category_top, category_mid, period),
            )
        conn.execute(
            "INSERT INTO budgets (category_top, category_mid, limit_amount, period, active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (category_top, category_mid, limit_amount, period, time.time()),
        )
        conn.commit()
        conn.close()

    def get_budgets(self) -> list[dict]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, category_top, category_mid, limit_amount, period, created_at "
            "FROM budgets WHERE active = 1 ORDER BY category_top, category_mid",
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

    def get_budget_status(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")

        conn = self._db()
        tx_rows = conn.execute(
            "SELECT t.description, t.amount, t.raw_data, m.category_top "
            "FROM transactions t "
            "LEFT JOIN merchants m ON t.description = m.raw_name "
            "WHERE t.date >= ? AND t.date <= ?",
            (month_start, today),
        ).fetchall()

        spending: dict[str, float] = {}
        for description, amount, raw_data_json, category_top in tx_rows:
            try:
                raw = json.loads(raw_data_json) if raw_data_json else {}
            except Exception:
                raw = {}
            if raw.get("credit_debit_indicator", "DBIT") == "CRDT":
                continue
            cat = category_top or "Other"
            spending[cat] = spending.get(cat, 0.0) + float(amount or 0)

        budget_rows = conn.execute(
            "SELECT category_top, category_mid, limit_amount, period "
            "FROM budgets WHERE active = 1",
        ).fetchall()
        conn.close()

        result = []
        for cat_top, cat_mid, limit_amount, period in budget_rows:
            spent = round(spending.get(cat_top, 0.0), 2)
            percent = round((spent / limit_amount) * 100, 1) if limit_amount > 0 else 0.0
            if percent < 80:
                status = "on_track"
            elif percent <= 100:
                status = "warning"
            else:
                status = "over"
            result.append({
                "category":     cat_top,
                "category_mid": cat_mid,
                "spent":        spent,
                "limit":        limit_amount,
                "period":       period,
                "percent":      percent,
                "status":       status,
            })
        return result

    def _db(self):
        _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_CACHE_DB))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                cached_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS merchants (
                raw_name      TEXT PRIMARY KEY,
                category_top  TEXT,
                category_mid  TEXT,
                category_leaf TEXT,
                resolved_name TEXT,
                mcc           TEXT,
                source        TEXT,
                created_at    REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_summaries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                summary    TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS syncs (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at           REAL,
                completed_at         REAL,
                accounts_synced      INTEGER,
                transactions_fetched INTEGER,
                new_transactions     INTEGER,
                errors               TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                account_uid    TEXT NOT NULL,
                transaction_id TEXT UNIQUE,
                date           TEXT NOT NULL,
                amount         REAL,
                currency       TEXT,
                description    TEXT,
                mcc            TEXT,
                category_top   TEXT,
                category_mid   TEXT,
                category_leaf  TEXT,
                raw_data       TEXT,
                created_at     REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                account_uid TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                fetched_at  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category_top TEXT NOT NULL,
                category_mid TEXT,
                limit_amount REAL NOT NULL,
                period       TEXT NOT NULL DEFAULT 'monthly',
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   REAL NOT NULL
            )
        """)
        conn.commit()
        return conn
