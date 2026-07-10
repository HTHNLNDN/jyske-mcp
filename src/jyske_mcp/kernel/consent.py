"""Enable Banking consent bootstrap — start/complete OAuth-style authorization.

Reuses jyske_mcp.kernel.auth for JWT signing; this module only builds/parses
the two Enable Banking HTTP calls needed to (re-)establish a session. Called
from app.py's /consent/* routes and from setup_consent.py.
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta

import requests

from jyske_mcp.kernel import scheduler_client
from jyske_mcp.kernel.auth import auth_headers, BASE_URL, HTTP_TIMEOUT, REDIRECT_URL

log = logging.getLogger("consent")

VALID_DAYS = 180
WARN_DAYS = 7
ASPSP_NAME = "Jyske Bank"
ASPSP_COUNTRY = "DK"

_PENDING_KEY = "consent:pending"


def start_authorization(storage) -> dict:
    """POST /auth to obtain a bank auth_url for the user to open, and stash
    the state we expect back on the callback."""
    state = str(uuid.uuid4())
    valid_until = (datetime.now(timezone.utc) + timedelta(days=VALID_DAYS)).isoformat()
    body = {
        "access": {"valid_until": valid_until},
        "aspsp": {"name": ASPSP_NAME, "country": ASPSP_COUNTRY},
        "state": state,
        "redirect_url": REDIRECT_URL,
        "psu_type": "personal",
    }
    r = requests.post(f"{BASE_URL}/auth", json=body, headers=auth_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    auth_url = r.json()["url"]

    storage.cache_set(_PENDING_KEY, {
        "state": state,
        "valid_until": valid_until,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"auth_url": auth_url, "state": state}


def complete_authorization(storage, code: str, expected_state: str) -> dict:
    """Exchange the callback code for a session, reconcile account uids
    against the previous session (if any), and persist the new session."""
    pending = storage.cache_get(_PENDING_KEY)
    if not pending or pending.get("state") != expected_state:
        raise ValueError("Missing or mismatched consent state — restart the reconnect flow.")

    old = storage.read_session_unchecked()

    r = requests.post(f"{BASE_URL}/sessions", json={"code": code}, headers=auth_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    session = r.json()

    new_accounts = session.get("accounts", [])

    # ── reconcile account uids across sessions ──────────────────────────────
    # Confirmed against Enable Banking's API reference (AccountResource
    # schema / example /sessions response): each account dict in the
    # /sessions response has identification_hash as a top-level field
    # alongside uid. If that ever changes upstream, skip reconciliation
    # rather than crash the auth flow.
    remapped: list[tuple[str, str]] = []
    old_accounts = (old or {}).get("accounts", [])
    old_by_hash = {}
    missing_hash = False
    for acc in old_accounts:
        h = acc.get("identification_hash")
        if h is None:
            missing_hash = True
            continue
        old_by_hash[h] = acc.get("uid")

    if old_accounts and not old_by_hash:
        log.warning(
            "No identification_hash found on any account in the prior session — "
            "skipping account uid reconciliation."
        )
    else:
        if missing_hash:
            log.warning("Some accounts in the prior session lack identification_hash; skipping those.")
        for acc in new_accounts:
            h = acc.get("identification_hash")
            new_uid = acc.get("uid")
            if h is None or new_uid is None:
                continue
            old_uid = old_by_hash.get(h)
            if old_uid and old_uid != new_uid:
                storage.remap_account_uid(old_uid, new_uid)
                remapped.append((old_uid, new_uid))

    storage.save_session({
        "session_id": session["session_id"],
        "accounts": new_accounts,
        "valid_until": pending["valid_until"],
    })

    # clear pending state now that it's been consumed
    storage.cache_set(_PENDING_KEY, None)

    # best-effort kick of a sync — the daily cron catches up regardless
    try:
        resp = scheduler_client.trigger_sync(None, timeout=2)
        if resp.status_code >= 300:
            log.warning("sync trigger returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("sync trigger failed: %s", e)

    return {"status": "ok", "accounts": new_accounts, "remapped": remapped}
