"""
Shared pytest fixtures.

jyske_mcp/auth.py reads ENABLE_BANKING_APP_ID / ENABLE_BANKING_REDIRECT_URL from
os.environ and .read_text()s the file at ENABLE_BANKING_PRIVATE_KEY_PATH —
all AT IMPORT TIME. Any test module that (transitively) imports jyske_mcp.auth
(jyske_mcp.jobs.sync, jyske_mcp.jobs.scheduler, ...) needs these env vars set before that import
happens, so they're set here at collection time, before test modules import
the app code under test.
"""
import os
import tempfile

os.environ.setdefault("ENABLE_BANKING_APP_ID", "test-app-id")
os.environ.setdefault("ENABLE_BANKING_REDIRECT_URL", "https://example.test/callback")

if "ENABLE_BANKING_PRIVATE_KEY_PATH" not in os.environ:
    _dummy_key = tempfile.NamedTemporaryFile(
        mode="w", suffix=".key", delete=False, prefix="dummy-eb-key-"
    )
    _dummy_key.write("not-a-real-key — never parsed unless make_token() runs")
    _dummy_key.close()
    os.environ["ENABLE_BANKING_PRIVATE_KEY_PATH"] = _dummy_key.name

import pytest


@pytest.fixture
def patched_auth_headers(monkeypatch):
    """Bypass make_token() (would fail against the dummy key above)."""
    import jyske_mcp.jobs.sync as sync

    monkeypatch.setattr(sync, "auth_headers", lambda: {})
    return sync
