"""
Covers the chat.log/sync.log redaction + rotation + file-permission hardening
in jyske_mcp/config.py: secure_config_files() must chmod every listed
sensitive file to 0600, and _SecureRotatingFileHandler must open (and reopen,
e.g. after a rollover) its log file at 0600 too. Pure filesystem tests — no
app boot, no network.
"""
import logging
import os
import stat
from pathlib import Path

import jyske_mcp.config as config


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_secure_config_files_chmods_existing_files(tmp_path, monkeypatch):
    db_file = tmp_path / "cache.db"
    session_file = tmp_path / "session.json"
    chat_log = tmp_path / "chat.log"
    sync_log = tmp_path / "sync.log"
    env_file = tmp_path / ".env"

    for f in (db_file, session_file, chat_log, sync_log, env_file):
        f.write_text("x")
        f.chmod(0o644)  # start deliberately loose

    monkeypatch.setattr(config, "SECURE_FILES", (db_file, session_file, chat_log, sync_log, env_file))

    config.secure_config_files()

    for f in (db_file, session_file, chat_log, sync_log, env_file):
        assert _mode(f) == 0o600


def test_secure_config_files_skips_missing_files_without_raising(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(config, "SECURE_FILES", (missing,))

    # Must not raise even though nothing exists yet.
    config.secure_config_files()
    assert not missing.exists()


def test_secure_config_files_is_idempotent(tmp_path, monkeypatch):
    db_file = tmp_path / "cache.db"
    db_file.write_text("x")
    monkeypatch.setattr(config, "SECURE_FILES", (db_file,))

    config.secure_config_files()
    config.secure_config_files()

    assert _mode(db_file) == 0o600


def test_secure_rotating_handler_opens_file_at_0600(tmp_path):
    log_path = tmp_path / "sub" / "chat.log"
    handler = config.secure_rotating_handler(log_path, "%(message)s")
    try:
        logger = logging.getLogger("test.secure_rotating_handler")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("hello")

        assert log_path.exists()
        assert _mode(log_path) == 0o600
    finally:
        handler.close()


def test_secure_rotating_handler_uses_configured_rotation_policy(tmp_path):
    log_path = tmp_path / "chat.log"
    handler = config.secure_rotating_handler(log_path, "%(message)s")
    try:
        assert handler.maxBytes == config.LOG_MAX_BYTES
        assert handler.backupCount == config.LOG_BACKUP_COUNT
    finally:
        handler.close()


def test_rotated_backup_file_is_0600(tmp_path):
    """After a rollover, the freshly (re)opened base file — and therefore the
    just-renamed backup, since os.rename() preserves mode — must both be
    0600. maxBytes is tiny here to force an immediate rollover."""
    log_path = tmp_path / "chat.log"
    handler = config._SecureRotatingFileHandler(str(log_path), maxBytes=1, backupCount=3)
    try:
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test.rotated_backup")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("first message forces a rollover on the next write")
        logger.info("second message lands after rollover")

        backup = tmp_path / "chat.log.1"
        assert backup.exists()
        assert _mode(backup) == 0o600
        assert _mode(log_path) == 0o600
    finally:
        handler.close()
