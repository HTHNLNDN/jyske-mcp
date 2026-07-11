"""
Tests for the SCHEDULER_SECRET auth guard on jyske_mcp/jobs/scheduler.py's internal
FastAPI routes (/sync/trigger, /tips/trigger, /sync/status).

Uses a plain TestClient(sched.app) — NOT the `with TestClient(...) as client`
context-manager form, since that runs the app's lifespan and would start the
real BackgroundScheduler with live cron jobs (daily sync/evals/tips) firing
against real Enable Banking credentials. Plain TestClient skips lifespan and
only exercises routes/dependencies, which is all the auth layer under test
needs.

Business logic (run_sync/snapshot_budget_history/run_tips/Storage.get_last_sync)
is monkeypatched out so these tests only assert on the auth dependency's
behavior, not on sync/tips/status internals (covered elsewhere).
"""
import pytest
from fastapi.testclient import TestClient

import jyske_mcp.jobs.scheduler as sched
import jyske_mcp.slices.finance.storage

client = TestClient(sched.app)

ROUTES = [
    ("post", "/sync/trigger"),
    ("post", "/tips/trigger"),
    ("get", "/sync/status"),
]


@pytest.fixture(autouse=True)
def _patch_business_logic(monkeypatch):
    """Isolate the auth dependency from the route bodies it guards."""
    monkeypatch.setattr(sched, "run_sync", lambda *a, **k: None)
    # Post-sync finance hook, called right after run_sync in the same job
    # (jyske_mcp.jobs.scheduler._sync_worker) — must be stubbed out here too,
    # or a real /sync/trigger call in this test would hit the real cache.db.
    monkeypatch.setattr(sched, "snapshot_budget_history", lambda *a, **k: None)
    monkeypatch.setattr(sched, "run_tips", lambda: None)
    monkeypatch.setattr(
        jyske_mcp.slices.finance.storage.Storage, "get_last_sync", lambda self: None
    )


def _call(method, path, headers=None):
    # /sync/trigger now takes a (fully-optional) JSON body — pass an empty
    # object so requests that make it past the auth dependency don't 422 on
    # a missing body instead of exercising the auth guard under test.
    kwargs = {"headers": headers or {}}
    if path == "/sync/trigger":
        kwargs["json"] = {}
    return getattr(client, method)(path, **kwargs)


@pytest.mark.parametrize("method,path", ROUTES)
def test_missing_header_rejected(monkeypatch, method, path):
    monkeypatch.setenv("SCHEDULER_SECRET", "correct-horse-battery-staple")
    resp = _call(method, path)
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_wrong_header_rejected(monkeypatch, method, path):
    monkeypatch.setenv("SCHEDULER_SECRET", "correct-horse-battery-staple")
    resp = _call(method, path, headers={"X-Scheduler-Secret": "wrong-secret"})
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_correct_header_accepted(monkeypatch, method, path):
    monkeypatch.setenv("SCHEDULER_SECRET", "correct-horse-battery-staple")
    resp = _call(method, path, headers={"X-Scheduler-Secret": "correct-horse-battery-staple"})
    if path == "/sync/trigger":
        # Now spawns the sync asynchronously (single-owner change) instead of
        # running it inline — 202 accepted, not 200.
        assert resp.status_code == 202
    else:
        assert resp.status_code == 200


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("secret_env", ["", None])
def test_unset_or_empty_secret_fails_closed(monkeypatch, method, path, secret_env):
    if secret_env is None:
        monkeypatch.delenv("SCHEDULER_SECRET", raising=False)
    else:
        monkeypatch.setenv("SCHEDULER_SECRET", secret_env)
    resp = _call(method, path, headers={"X-Scheduler-Secret": "anything"})
    assert resp.status_code == 503
