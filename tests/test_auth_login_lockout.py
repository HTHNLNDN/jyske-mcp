"""
Characterization tests for /auth/login (jyske_mcp/web/app.py): the PIN
check, the wrong-PIN attempt countdown, and the 3-strike lockout. Pure
in-memory state (module globals _failed_attempts/_lockout_until) — no DB,
no Enable Banking, no LLM involved anywhere in this file.

Known, already-tracked and intentionally NOT this file's concern to fix:
the PIN compare below is a plain `==`, not constant-time — see
.agent/epics/vsa-restructure.md's "Related backlog" (PIN auth hardening).
This suite pins today's behavior, bug-for-bug, as the baseline the VSA
migration is diffed against.

_failed_attempts/_lockout_until are process-wide module globals, mutated
via `global` inside login() — not request-scoped. The autouse fixture below
resets them to a known-clean state before AND after every test in this
file, so (a) tests here never see state left over by each other, and
(b) this file can never leak a dirty lockout into some other test file that
also happens to hit /auth/login (see tests/test_budget_and_goal_endpoints.py,
tests/test_chat_tool_dispatch.py) regardless of execution order.
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

import jyske_mcp.web.app as app_module

CORRECT_PIN = os.environ["APP_PIN"]
WRONG_PIN = "9999" if CORRECT_PIN != "9999" else "8888"


@pytest.fixture(autouse=True)
def _reset_login_state():
    app_module._failed_attempts = 0
    app_module._lockout_until = 0.0
    yield
    app_module._failed_attempts = 0
    app_module._lockout_until = 0.0


@pytest.fixture
def client():
    return TestClient(app_module.app)


def test_correct_pin_returns_ok_and_sets_session_cookie(client):
    resp = client.post("/auth/login", json={"pin": CORRECT_PIN})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert app_module.SESSION_COOKIE in resp.cookies


def test_wrong_pin_returns_401_with_remaining_attempts_countdown(client):
    resp1 = client.post("/auth/login", json={"pin": WRONG_PIN})
    assert resp1.status_code == 401
    assert resp1.json() == {"detail": "Invalid PIN. 2 attempt(s) remaining."}

    resp2 = client.post("/auth/login", json={"pin": WRONG_PIN})
    assert resp2.status_code == 401
    assert resp2.json() == {"detail": "Invalid PIN. 1 attempt(s) remaining."}


def test_correct_pin_after_one_failed_attempt_resets_the_counter(client):
    client.post("/auth/login", json={"pin": WRONG_PIN})
    assert app_module._failed_attempts == 1

    resp = client.post("/auth/login", json={"pin": CORRECT_PIN})

    assert resp.status_code == 200
    assert app_module._failed_attempts == 0


def test_third_consecutive_wrong_pin_locks_out_for_60s(client):
    client.post("/auth/login", json={"pin": WRONG_PIN})
    client.post("/auth/login", json={"pin": WRONG_PIN})
    resp3 = client.post("/auth/login", json={"pin": WRONG_PIN})

    assert resp3.status_code == 429
    assert resp3.json() == {"detail": "Too many failed attempts. Locked out for 60s."}
    # the 3-strike counter itself resets alongside triggering the lockout
    assert app_module._failed_attempts == 0
    assert app_module._lockout_until > time.time()


def test_login_attempt_during_active_lockout_is_rejected_regardless_of_pin(client):
    app_module._lockout_until = time.time() + 45

    resp = client.post("/auth/login", json={"pin": CORRECT_PIN})

    assert resp.status_code == 429
    assert "Too many failed attempts. Try again in" in resp.json()["detail"]


def test_correct_pin_after_lockout_expires_succeeds(client):
    app_module._lockout_until = time.time() - 1  # already expired

    resp = client.post("/auth/login", json={"pin": CORRECT_PIN})

    assert resp.status_code == 200


def test_protected_route_rejects_missing_session_cookie(client):
    resp = client.get("/goals")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Unauthorized"}


def test_protected_route_accepts_valid_session_cookie(client, full_schema_storage):
    login_resp = client.post("/auth/login", json={"pin": CORRECT_PIN})
    assert login_resp.status_code == 200

    resp = client.get("/goals")

    assert resp.status_code == 200
    assert resp.json() == {"goals": []}


def test_logout_clears_the_session_cookie(client, full_schema_storage):
    client.post("/auth/login", json={"pin": CORRECT_PIN})
    assert client.get("/goals").status_code == 200

    logout_resp = client.post("/auth/logout")
    assert logout_resp.status_code == 200

    resp = client.get("/goals")
    assert resp.status_code == 401
