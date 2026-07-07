"""
Tests for the SCHEDULER_SECRET auth guard on cron/scheduler.py's internal
FastAPI routes (/sync/trigger, /tips/trigger, /sync/status).

Uses a plain TestClient(sched.app) — NOT the `with TestClient(...) as client`
context-manager form, since that runs the app's lifespan and would start the
real BackgroundScheduler with live cron jobs (daily sync/evals/tips) firing
against real Enable Banking credentials. Plain TestClient skips lifespan and
only exercises routes/dependencies, which is all the auth layer under test
needs.

Business logic (run_sync/run_tips/Storage.get_last_sync) is monkeypatched out
so these tests only assert on the auth dependency's behavior, not on
sync/tips/status internals (covered elsewhere).
"""
import pytest
from fastapi.testclient import TestClient

import cron.scheduler as sched
import lib.storage

client = TestClient(sched.app)

ROUTES = [
    ("post", "/sync/trigger"),
    ("post", "/tips/trigger"),
    ("get", "/sync/status"),
]


@pytest.fixture(autouse=True)
def _patch_business_logic(monkeypatch):
    """Isolate the auth dependency from the route bodies it guards."""
    monkeypatch.setattr(sched, "run_sync", lambda: None)
    monkeypatch.setattr(sched, "run_tips", lambda: None)
    monkeypatch.setattr(lib.storage.Storage, "get_last_sync", lambda self: None)


def _call(method, path, headers=None):
    return getattr(client, method)(path, headers=headers or {})


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
