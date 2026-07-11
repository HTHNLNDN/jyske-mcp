"""
Shared pytest fixtures.

jyske_mcp/kernel/auth.py reads ENABLE_BANKING_APP_ID / ENABLE_BANKING_REDIRECT_URL from
os.environ and .read_text()s the file at ENABLE_BANKING_PRIVATE_KEY_PATH —
all AT IMPORT TIME. Any test module that (transitively) imports jyske_mcp.kernel.auth
(jyske_mcp.kernel.sync, jyske_mcp.jobs.scheduler, ...) needs these env vars set before that import
happens, so they're set here at collection time, before test modules import
the app code under test.

jyske_mcp/web/app.py similarly reads APP_PIN / SESSION_SECRET from os.environ
at IMPORT TIME (`os.environ["APP_PIN"]` — no default, KeyErrors if unset).
Same fix, same reasoning: set dummy values here, before any test module
imports jyske_mcp.web.app. Tests that need to log in via /auth/login read
os.environ["APP_PIN"] back out rather than hardcoding "0000", so they keep
working even if this default ever changes.
"""
import os
import sqlite3
import tempfile
import time

os.environ.setdefault("ENABLE_BANKING_APP_ID", "test-app-id")
os.environ.setdefault("ENABLE_BANKING_REDIRECT_URL", "https://example.test/callback")

if "ENABLE_BANKING_PRIVATE_KEY_PATH" not in os.environ:
    _dummy_key = tempfile.NamedTemporaryFile(
        mode="w", suffix=".key", delete=False, prefix="dummy-eb-key-"
    )
    _dummy_key.write("not-a-real-key — never parsed unless make_token() runs")
    _dummy_key.close()
    os.environ["ENABLE_BANKING_PRIVATE_KEY_PATH"] = _dummy_key.name

os.environ.setdefault("APP_PIN", "0000")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-do-not-use-in-prod")

import pytest


@pytest.fixture
def patched_auth_headers(monkeypatch):
    """Bypass make_token() (would fail against the dummy key above)."""
    import jyske_mcp.kernel.sync as sync

    monkeypatch.setattr(sync, "auth_headers", lambda: {})
    return sync


# ── full-schema Storage fixture ─────────────────────────────────────────────
# Most fixtures in this suite create a narrow, single-concern DDL subset
# (see e.g. tests/test_sum_spending.py, tests/test_goal_pace.py) matching
# just the tables that one file's target function touches. Some
# characterization tests need to exercise many domains at once (the full
# 23-tool MCP dispatch surface, an HTTP endpoint that joins several tables,
# consent's session/cache persistence) — for those, this fixture builds the
# FULL current migrated schema instead of yet another narrow subset. Table
# DDL mirrors migrations/versions/*.py exactly, through head revision
# 968eeaea94c3 (968eeaea94c3 itself only flips journal_mode, no DDL change).
_FULL_SCHEMA_DDL = (
    """
    CREATE TABLE cache (
        key TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        cached_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE merchants (
        raw_name      TEXT PRIMARY KEY,
        category_top  TEXT,
        category_mid  TEXT,
        category_leaf TEXT,
        resolved_name TEXT,
        mcc           TEXT,
        source        TEXT,
        created_at    REAL
    )
    """,
    """
    CREATE TABLE session_summaries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        summary    TEXT NOT NULL,
        created_at REAL NOT NULL,
        agent_id   TEXT NOT NULL DEFAULT 'finance'
    )
    """,
    """
    CREATE TABLE user_profile (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE syncs (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at           REAL,
        completed_at         REAL,
        accounts_synced      INTEGER,
        transactions_fetched INTEGER,
        new_transactions     INTEGER,
        errors               TEXT
    )
    """,
    """
    CREATE TABLE transactions (
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
        created_at     REAL NOT NULL,
        direction      TEXT
    )
    """,
    "CREATE INDEX ix_transactions_date ON transactions(date)",
    """
    CREATE TABLE balances (
        account_uid TEXT PRIMARY KEY,
        data        TEXT NOT NULL,
        fetched_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE budgets (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        category_top TEXT NOT NULL,
        category_mid TEXT,
        limit_amount REAL NOT NULL,
        period       TEXT NOT NULL DEFAULT 'monthly',
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   REAL NOT NULL,
        agent_id     TEXT NOT NULL DEFAULT 'finance'
    )
    """,
    """
    CREATE TABLE goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL DEFAULT 'finance',
        name TEXT NOT NULL,
        target_amount REAL,
        current_amount REAL DEFAULT 0,
        purpose TEXT,
        deadline TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE onboarding (
        agent_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL DEFAULT 'income',
        income REAL,
        income_day INTEGER,
        fixed_costs TEXT,
        savings_monthly REAL,
        savings_purpose TEXT,
        savings_target REAL,
        savings_deadline TEXT,
        budget_style TEXT DEFAULT 'honest',
        completed_at REAL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE budget_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL DEFAULT 'finance',
        category_top TEXT NOT NULL,
        period TEXT NOT NULL,
        limit_amount REAL NOT NULL,
        actual_amount REAL NOT NULL,
        variance REAL NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE recurring_charge_status (
        merchant     TEXT NOT NULL,
        currency     TEXT NOT NULL,
        status       TEXT NOT NULL,
        confirmed_at REAL NOT NULL,
        PRIMARY KEY (merchant, currency)
    )
    """,
    """
    CREATE TABLE tips (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id              TEXT NOT NULL DEFAULT 'finance',
        created_at            REAL NOT NULL,
        tip_date              TEXT NOT NULL,
        window_from           TEXT NOT NULL,
        window_to             TEXT NOT NULL,
        tip_text              TEXT NOT NULL,
        subject_key           TEXT,
        category_top          TEXT,
        based_on              TEXT,
        signals_json          TEXT NOT NULL,
        model                 TEXT NOT NULL,
        prompt_version        TEXT NOT NULL,
        feedback_status       TEXT NOT NULL DEFAULT 'pending',
        feedback_reason_code  TEXT,
        feedback_reason_text  TEXT,
        feedback_source       TEXT,
        feedback_at           REAL,
        UNIQUE(agent_id, tip_date)
    )
    """,
    "CREATE INDEX ix_tips_tip_date ON tips(tip_date)",
    """
    CREATE TABLE provider_keys (
        provider   TEXT PRIMARY KEY,
        api_key    TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE agents (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT,
        model       TEXT,
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    )
    """,
)


@pytest.fixture
def full_schema_storage(monkeypatch, tmp_path):
    """
    Storage backed by a temp SQLite DB carrying the FULL current migrated
    schema (see _FULL_SCHEMA_DDL above), with the 'finance' agent row seeded
    the same way a8f633b0783b_add_provider_keys_and_agents_tables.py's data
    migration does. Redirects the kernel storage module's globals (_CACHE_DB,
    CONFIG_DIR, _SESSION_FILE) the same way every other fixture in this
    suite does — _db() and get_session()/save_session() (both defined in
    jyske_mcp.kernel.storage, which every Storage/FinanceStorage/
    KernelStorage instance inherits) re-read these at call time, so this
    transparently redirects every such instance (including
    jyske_mcp.slices.finance.tools's module-level one) for
    the duration of the test.
    """
    import jyske_mcp.kernel.storage as storage_module

    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    for ddl in _FULL_SCHEMA_DDL:
        conn.execute(ddl)
    now = time.time()
    conn.execute(
        "INSERT INTO agents (id, name, description, model, created_at, updated_at) "
        "VALUES ('finance', 'Finance Agent', 'Personal finance companion', NULL, ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage_module, "_CACHE_DB", db_path)
    # Avoid touching ~/.config/mcp-bank in _db()'s CONFIG_DIR.mkdir/chmod.
    monkeypatch.setattr(storage_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(storage_module, "_SESSION_FILE", tmp_path / "session.json")

    from jyske_mcp.slices.finance.storage import Storage
    return Storage()
