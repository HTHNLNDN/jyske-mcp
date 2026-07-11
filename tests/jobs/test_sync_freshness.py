"""
Covers the stale-sync alerting added around jyske_mcp.kernel.sync.is_sync_stale
and jyske_mcp.jobs.scheduler.check_sync_freshness.
"""
import logging

import jyske_mcp.kernel.sync as sync


def test_is_sync_stale_none_last_sync_is_stale():
    assert sync.is_sync_stale(None, now=1_000_000.0) is True


def test_is_sync_stale_27h_ago_is_stale():
    now = 1_000_000.0
    last = {"completed_at": now - 27 * 3600}
    assert sync.is_sync_stale(last, now) is True


def test_is_sync_stale_1h_ago_is_fresh():
    now = 1_000_000.0
    last = {"completed_at": now - 1 * 3600}
    assert sync.is_sync_stale(last, now) is False


def test_is_sync_stale_boundary_just_under_threshold_is_fresh():
    now = 1_000_000.0
    last = {"completed_at": now - (sync.STALE_SYNC_HOURS * 3600 - 1)}
    assert sync.is_sync_stale(last, now) is False


def test_is_sync_stale_boundary_just_over_threshold_is_stale():
    now = 1_000_000.0
    last = {"completed_at": now - (sync.STALE_SYNC_HOURS * 3600 + 1)}
    assert sync.is_sync_stale(last, now) is True


def test_check_sync_freshness_logs_error_when_stale(monkeypatch, caplog):
    import jyske_mcp.jobs.scheduler as sched
    from jyske_mcp.slices.finance.storage import Storage

    monkeypatch.setattr(Storage, "get_last_sync", lambda self: None)

    with caplog.at_level(logging.ERROR, logger="scheduler"):
        sched.check_sync_freshness()

    assert any(
        "no sync has ever completed" in rec.message
        for rec in caplog.records
        if rec.levelno == logging.ERROR
    )


def test_check_sync_freshness_logs_error_when_stale_with_prior_sync(monkeypatch, caplog):
    import jyske_mcp.jobs.scheduler as sched
    import time as time_mod
    from jyske_mcp.slices.finance.storage import Storage

    stale_completed_at = time_mod.time() - 27 * 3600
    monkeypatch.setattr(
        Storage, "get_last_sync", lambda self: {"completed_at": stale_completed_at}
    )

    with caplog.at_level(logging.ERROR, logger="scheduler"):
        sched.check_sync_freshness()

    assert any(
        "last sync completed" in rec.message and "may be wedged" in rec.message
        for rec in caplog.records
        if rec.levelno == logging.ERROR
    )


def test_check_sync_freshness_silent_when_fresh(monkeypatch, caplog):
    import jyske_mcp.jobs.scheduler as sched
    import time as time_mod
    from jyske_mcp.slices.finance.storage import Storage

    fresh_completed_at = time_mod.time() - 1 * 3600
    monkeypatch.setattr(
        Storage, "get_last_sync", lambda self: {"completed_at": fresh_completed_at}
    )

    with caplog.at_level(logging.ERROR, logger="scheduler"):
        sched.check_sync_freshness()

    assert not [rec for rec in caplog.records if rec.levelno == logging.ERROR]
