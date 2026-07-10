"""
Characterization tests for jyske_mcp/kernel/consent.py's start_authorization /
complete_authorization — the Enable Banking OAuth-style consent bootstrap.

requests.post (the two live Enable Banking calls, /auth and /sessions) is
mocked in every test below via monkeypatching consent_lib.requests.post —
this file makes ZERO live Enable Banking calls. consent_lib.auth_headers is
also patched, mirroring tests/conftest.py's patched_auth_headers fixture
(that fixture itself only patches jyske_mcp.kernel.sync's own `auth_headers`
name binding, a separate import from consent.py's — see jyske_mcp/kernel/consent.py:
`from jyske_mcp.kernel.auth import auth_headers, ...` — so it doesn't cover this
module and needs its own patch here). complete_authorization's best-effort
post-auth sync kick (scheduler_client.trigger_sync, to the internal :8081
scheduler process) is patched too, so no HTTP attempt — even a doomed
loopback one — escapes this file either.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import jyske_mcp.kernel.consent as consent_lib


@pytest.fixture(autouse=True)
def _patch_consent_dependencies(monkeypatch):
    monkeypatch.setattr(consent_lib, "auth_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(
        consent_lib.scheduler_client, "trigger_sync",
        MagicMock(return_value=MagicMock(status_code=202)),
    )


def _fake_post_response(json_body: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    return resp


def test_start_authorization_posts_expected_body_and_persists_pending_state(monkeypatch, full_schema_storage):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _fake_post_response({"url": "https://bank.example/authorize?x=1"})

    monkeypatch.setattr(consent_lib.requests, "post", fake_post)

    result = consent_lib.start_authorization(full_schema_storage)

    assert result["auth_url"] == "https://bank.example/authorize?x=1"
    assert "state" in result

    assert captured["url"] == f"{consent_lib.BASE_URL}/auth"
    assert captured["timeout"] == consent_lib.HTTP_TIMEOUT
    body = captured["json"]
    assert body["aspsp"] == {"name": "Jyske Bank", "country": "DK"}
    assert body["psu_type"] == "personal"
    assert body["redirect_url"] == consent_lib.REDIRECT_URL
    assert body["state"] == result["state"]

    pending = full_schema_storage.cache_get(consent_lib._PENDING_KEY)
    assert pending["state"] == result["state"]
    assert pending["valid_until"] == body["access"]["valid_until"]
    parsed_valid_until = datetime.fromisoformat(pending["valid_until"])
    assert (parsed_valid_until - datetime.now(timezone.utc)).days >= consent_lib.VALID_DAYS - 1


def test_complete_authorization_happy_path_saves_session_and_clears_pending(monkeypatch, full_schema_storage):
    state = "abc-state"
    full_schema_storage.cache_set(consent_lib._PENDING_KEY, {
        "state": state,
        "valid_until": "2027-01-01T00:00:00+00:00",
        "created_at": "2026-07-01T00:00:00+00:00",
    })

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url == f"{consent_lib.BASE_URL}/sessions"
        assert json == {"code": "auth-code-123"}
        return _fake_post_response({
            "session_id": "sess-xyz",
            "accounts": [{"uid": "acc-new", "product": "Checking", "identification_hash": "hash-1"}],
        })

    monkeypatch.setattr(consent_lib.requests, "post", fake_post)

    result = consent_lib.complete_authorization(full_schema_storage, "auth-code-123", state)

    assert result["status"] == "ok"
    assert result["remapped"] == []
    assert result["accounts"][0]["uid"] == "acc-new"

    saved = full_schema_storage.read_session_unchecked()
    assert saved["session_id"] == "sess-xyz"
    assert saved["valid_until"] == "2027-01-01T00:00:00+00:00"
    assert saved["accounts"] == [{"uid": "acc-new", "product": "Checking", "identification_hash": "hash-1"}]

    # pending state was consumed and cleared
    assert full_schema_storage.cache_get(consent_lib._PENDING_KEY) is None

    # best-effort post-auth sync kick fired exactly once
    consent_lib.scheduler_client.trigger_sync.assert_called_once()


def test_complete_authorization_rejects_mismatched_state(full_schema_storage):
    full_schema_storage.cache_set(consent_lib._PENDING_KEY, {"state": "expected", "valid_until": "x"})

    with pytest.raises(ValueError, match="Missing or mismatched consent state"):
        consent_lib.complete_authorization(full_schema_storage, "any-code", "wrong-state")


def test_complete_authorization_rejects_when_no_pending_state_exists(full_schema_storage):
    with pytest.raises(ValueError, match="Missing or mismatched consent state"):
        consent_lib.complete_authorization(full_schema_storage, "any-code", "whatever")


def test_complete_authorization_remaps_account_uid_via_identification_hash(monkeypatch, full_schema_storage):
    # Old session had one account (uid "old-uid") coming back under a new
    # uid but the same identification_hash -- cached data for it must move
    # to the new uid rather than being orphaned under the old one.
    full_schema_storage.save_session({
        "session_id": "sess-old",
        "accounts": [{"uid": "old-uid", "identification_hash": "hash-1"}],
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
    })
    full_schema_storage.store_balance("old-uid", {"balances": [{"balance_type": "closingBooked"}]})

    state = "reauth-state"
    full_schema_storage.cache_set(consent_lib._PENDING_KEY, {
        "state": state, "valid_until": "2027-01-01T00:00:00+00:00",
    })

    def fake_post(url, json=None, headers=None, timeout=None):
        return _fake_post_response({
            "session_id": "sess-new",
            "accounts": [{"uid": "new-uid", "identification_hash": "hash-1"}],
        })

    monkeypatch.setattr(consent_lib.requests, "post", fake_post)

    result = consent_lib.complete_authorization(full_schema_storage, "code", state)

    assert result["remapped"] == [("old-uid", "new-uid")]
    assert full_schema_storage.get_balances_cached("old-uid") is None
    assert full_schema_storage.get_balances_cached("new-uid") == {"balances": [{"balance_type": "closingBooked"}]}


def test_complete_authorization_skips_reconciliation_when_no_prior_session(monkeypatch, full_schema_storage):
    # No prior session at all -- old_accounts is empty, so reconciliation is
    # a no-op rather than an error, and remapped comes back empty.
    state = "first-auth-state"
    full_schema_storage.cache_set(consent_lib._PENDING_KEY, {
        "state": state, "valid_until": "2027-01-01T00:00:00+00:00",
    })

    monkeypatch.setattr(
        consent_lib.requests, "post",
        lambda *a, **k: _fake_post_response({
            "session_id": "sess-1",
            "accounts": [{"uid": "acc-1", "identification_hash": "hash-1"}],
        }),
    )

    result = consent_lib.complete_authorization(full_schema_storage, "code", state)

    assert result["remapped"] == []
    assert full_schema_storage.read_session_unchecked()["session_id"] == "sess-1"
