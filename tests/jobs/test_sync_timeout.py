"""
Covers HTTP_TIMEOUT wiring on the two Enable Banking GET call sites in
jyske_mcp/jobs/sync.py (transactions fetch and balances fetch), plus the
lock-release guarantee in jyske_mcp/jobs/scheduler.py when a fetch raises a
timeout instead of returning cleanly (i.e. the sync worker's `finally` must
still run and release `_sync_lock`, so the process never gets permanently
wedged after a slow/dead ASPSP).
"""
import time
from unittest.mock import MagicMock

import requests
from fastapi.testclient import TestClient

import jyske_mcp.jobs.sync as sync


def _fake_response(*, transactions=None, continuation_key=None, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json.return_value = {
        "transactions": transactions if transactions is not None else [],
        "continuation_key": continuation_key,
    }
    return r


def test_fetch_transactions_passes_http_timeout(monkeypatch, patched_auth_headers):
    page = _fake_response(transactions=[{"id": "a"}], continuation_key=None)
    mock_get = MagicMock(return_value=page)
    monkeypatch.setattr(sync._EB_SESSION, "get", mock_get)

    sync._fetch_transactions("acc1", "2026-01-01", "2026-07-07")

    assert mock_get.call_args.kwargs["timeout"] == sync.HTTP_TIMEOUT
    assert sync.HTTP_TIMEOUT == (5, 30)


def test_run_sync_balances_fetch_passes_http_timeout(monkeypatch, patched_auth_headers):
    page = _fake_response(transactions=[{"transaction_id": "a"}], continuation_key=None)
    mock_get = MagicMock(return_value=page)
    monkeypatch.setattr(sync._EB_SESSION, "get", mock_get)
    monkeypatch.setattr(sync, "categorize", lambda raw_name, mcc, storage: {
        "top": "Other", "mid": "Other", "leaf": "Other",
    })

    storage = MagicMock()
    storage.get_session.return_value = {"accounts": [{"uid": "acc1", "product": "Test"}]}
    storage.most_recent_transaction_date.return_value = None
    storage.balance_fetched_at.return_value = None  # stale -> balances GET fires
    storage.get_budget_status.return_value = []
    storage.backfill_categories.return_value = 0
    monkeypatch.setattr(sync, "Storage", MagicMock(return_value=storage))

    sync.run_sync()

    # both the transactions call and the balances call must have gone through
    # _EB_SESSION.get with the shared timeout.
    assert mock_get.call_count == 2
    for call in mock_get.call_args_list:
        assert call.kwargs["timeout"] == sync.HTTP_TIMEOUT
    balances_call = mock_get.call_args_list[1]
    assert "/accounts/acc1/balances" in balances_call.args[0]


def test_sync_worker_releases_lock_after_timeout(monkeypatch):
    """
    A ReadTimeout (or any exception) escaping run_sync() must still hit
    _sync_worker's `finally` and release _sync_lock — otherwise every
    subsequent trigger is rejected forever after one slow ASPSP call.
    """
    import jyske_mcp.jobs.scheduler as sched

    # plain constructor — never starts the real BackgroundScheduler
    TestClient(sched.app)

    def fake_run_sync(*a, **k):
        raise requests.exceptions.ReadTimeout("simulated ASPSP timeout")

    monkeypatch.setattr(sched, "run_sync", fake_run_sync)

    assert sched._start_sync() is True

    for _ in range(50):
        if not sched._sync_state["running"]:
            break
        time.sleep(0.1)
    assert sched._sync_state["running"] is False
    assert "simulated ASPSP timeout" in (sched._sync_state["error"] or "")

    # lock was released by _sync_worker's finally -> a second start succeeds
    assert sched._start_sync() is True
