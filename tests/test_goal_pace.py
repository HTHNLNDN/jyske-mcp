"""
Unit tests for the goal_pace MCP tool (jyske_mcp/mcp/server.py) — pacing math
(pct_complete/status/required_daily/required_monthly/projected_completion_date)
computed over goals read from Storage.get_goals().

Uses a real temporary SQLite DB with the `goals` table DDL from
migrations/versions/aa1665106662_add_goals_table.py — Storage no longer
creates tables itself, so the fixture must. Both a fresh Storage() and
jyske_mcp.mcp.server's module-global `storage` read the same DB, since
_db() re-reads storage_module._CACHE_DB on every call — monkeypatching that
global (and CONFIG_DIR) is enough to redirect both (same pattern as
tests/test_mixed_currency_no_blend.py).

goal_pace has no injectable "now" (it calls datetime.now(timezone.utc)
internally), so every fixture here anchors created_at/deadline to exact
midnight UTC of a day N calendar-days from *today*. Because
(now - midnight_N_days_ago).days floors to exactly N regardless of what
time of day the test happens to run at, days_elapsed and days_total come
out fully deterministic with zero flakiness. days_remaining (which is
midnight_deadline - now, i.e. NOT floor-clean against a midnight-anchored
start) is intentionally never asserted to an exact value — required_daily/
required_monthly are instead checked for self-consistency against whatever
days_remaining the tool actually returns.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import jyske_mcp.storage as storage_module
from jyske_mcp.storage import Storage

_GOALS_DDL = """
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
"""


@pytest.fixture
def storage(monkeypatch, tmp_path):
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_GOALS_DDL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage_module, "_CACHE_DB", db_path)
    # Avoid touching ~/.config/mcp-bank in _db()'s CONFIG_DIR.mkdir/chmod.
    monkeypatch.setattr(storage_module, "CONFIG_DIR", tmp_path)

    return Storage()


def _midnight_utc(days_from_today: int) -> datetime:
    """Midnight UTC of (today + days_from_today), where "today" is the
    caller's actual current UTC date. Negative values -> a past day."""
    today = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return datetime(today.year, today.month, today.day, tzinfo=timezone.utc)


def _insert_goal(
    storage, *, name, target_amount, current_amount, deadline, created_at, agent_id="finance"
):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO goals "
        "(agent_id, name, target_amount, current_amount, purpose, deadline, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (agent_id, name, target_amount, current_amount, "test purpose", deadline, created_at, created_at),
    )
    goal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return goal_id


def _call_goal_pace(goal_id: int = 0):
    import json
    import jyske_mcp.mcp.server as server

    return json.loads(server.goal_pace(goal_id=goal_id))


def test_on_track_status(storage):
    # Created 30 days ago, deadline 60 days out -> days_total=90,
    # expected_now = target * 30/90 = target/3. current == expected_now
    # exactly, so it lands inside the +/-5% tolerance band.
    created_at = _midnight_utc(-30)
    deadline = _midnight_utc(60)
    _insert_goal(
        storage, name="Emergency fund", target_amount=900.0, current_amount=300.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    results = _call_goal_pace()
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "on_track"
    assert r["pct_complete"] == round(300.0 / 900.0 * 100, 1)


def test_behind_status(storage):
    created_at = _midnight_utc(-30)
    deadline = _midnight_utc(60)
    # expected_now == 300; well below the 285 (5% under) floor.
    _insert_goal(
        storage, name="Behind goal", target_amount=900.0, current_amount=100.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    results = _call_goal_pace()
    assert results[0]["status"] == "behind"


def test_ahead_status(storage):
    created_at = _midnight_utc(-30)
    deadline = _midnight_utc(60)
    # expected_now == 300; well above the 315 (5% over) ceiling.
    _insert_goal(
        storage, name="Ahead goal", target_amount=900.0, current_amount=500.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    results = _call_goal_pace()
    assert results[0]["status"] == "ahead"


def test_required_daily_and_monthly_math_is_self_consistent(storage):
    created_at = _midnight_utc(-30)
    deadline = _midnight_utc(60)
    _insert_goal(
        storage, name="Required math goal", target_amount=900.0, current_amount=300.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    r = _call_goal_pace()[0]
    amount_remaining = round(900.0 - 300.0, 2)
    days_remaining = r["days_remaining"]
    assert days_remaining is not None and days_remaining > 0

    expected_daily = round(amount_remaining / days_remaining, 2)
    expected_monthly = round(expected_daily * 30.4, 2)
    assert r["required_daily"] == expected_daily
    assert r["required_monthly"] == expected_monthly


def test_projected_completion_date(storage):
    # No deadline -- created 500 days ago, current progressing linearly at
    # 250/500 = 0.5/day -> days_to_target = 1000 / 0.5 = 2000 days from
    # created_at exactly (both created_at and the expectation below are
    # midnight-anchored, so this is an exact, non-flaky equality).
    created_at = _midnight_utc(-500)
    _insert_goal(
        storage, name="Long horizon goal", target_amount=1000.0, current_amount=250.0,
        deadline="", created_at=created_at.timestamp(),
    )

    r = _call_goal_pace()[0]
    expected = (created_at + timedelta(days=2000.0)).strftime("%Y-%m-%d")
    assert r["projected_completion_date"] == expected


def test_already_complete_goal(storage):
    # current >= target takes priority over every other status, even with a
    # deadline already in the past (which would otherwise read "overdue").
    created_at = _midnight_utc(-100)
    deadline = _midnight_utc(-10)
    _insert_goal(
        storage, name="Done goal", target_amount=500.0, current_amount=500.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    r = _call_goal_pace()[0]
    assert r["status"] == "complete"
    assert r["pct_complete"] == 100.0


def test_zero_elapsed_no_div_by_zero(storage):
    # created_at == today's midnight (days_elapsed == 0) and deadline == the
    # same day (days_total == 0) -- both are guarded (`if days_elapsed and
    # days_elapsed > 0`, `if days_total > 0`) specifically so this doesn't
    # raise ZeroDivisionError. Not asserting on the exact status (deadline
    # "today" is already in the past relative to "now" mid-day, so this
    # legitimately reads overdue) -- the point of this test is that it
    # doesn't crash and required_daily/monthly/projected_completion_date
    # stay None rather than dividing by zero.
    created_at = _midnight_utc(0)
    deadline = _midnight_utc(0)
    _insert_goal(
        storage, name="Same day goal", target_amount=100.0, current_amount=10.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    r = _call_goal_pace()[0]
    assert r["required_daily"] is None
    assert r["required_monthly"] is None
    assert r["projected_completion_date"] is None


def test_goal_id_filters_to_single_goal(storage):
    created_at = _midnight_utc(-30)
    deadline = _midnight_utc(60)
    gid1 = _insert_goal(
        storage, name="Goal one", target_amount=900.0, current_amount=300.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )
    _insert_goal(
        storage, name="Goal two", target_amount=500.0, current_amount=100.0,
        deadline=deadline.strftime("%Y-%m-%d"), created_at=created_at.timestamp(),
    )

    results = _call_goal_pace(goal_id=gid1)
    assert len(results) == 1
    assert results[0]["goal_id"] == gid1
