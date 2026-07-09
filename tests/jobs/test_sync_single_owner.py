"""
Tests that jyske_mcp/jobs/scheduler.py is the single owner of sync execution: a
manual /sync/trigger while one is already in flight must be rejected (409)
rather than starting a second concurrent run_sync().

Uses a plain TestClient(sched.app) — NOT the `with TestClient(...) as client`
context-manager form — to avoid starting the real BackgroundScheduler (see
test_scheduler_auth.py's module docstring for why).
"""
import threading
import time

from fastapi.testclient import TestClient

import jyske_mcp.jobs.scheduler as sched

client = TestClient(sched.app)


def test_concurrent_trigger_rejected_while_sync_running(monkeypatch):
    monkeypatch.setenv("SCHEDULER_SECRET", "s")

    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake(*a, **k):
        calls.append(1)
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(sched, "run_sync", fake)

    headers = {"X-Scheduler-Secret": "s"}

    resp1 = client.post("/sync/trigger", json={}, headers=headers)
    assert resp1.status_code == 202
    started.wait(timeout=2)

    resp2 = client.post("/sync/trigger", json={}, headers=headers)
    assert resp2.status_code == 409
    assert resp2.json() == {"status": "already_running"}

    release.set()
    # poll until the background worker has finished and released the lock
    for _ in range(50):
        if not sched._sync_state["running"]:
            break
        time.sleep(0.1)
    assert sched._sync_state["running"] is False

    assert len(calls) == 1
