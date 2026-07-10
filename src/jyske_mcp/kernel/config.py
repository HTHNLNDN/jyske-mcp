import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Repo root. Valid because this package is installed editable (pip install -e .)
# by design — alembic.ini, static/, and .env are repo files, not package data,
# and everything that needs them anchors here instead of counting parents.
# This module lives at src/jyske_mcp/kernel/config.py, so parents[3] is the
# repo root (parents[0]=kernel, [1]=jyske_mcp, [2]=src, [3]=repo root).
ROOT_DIR = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT_DIR / ".env"

CONFIG_DIR = Path("~/.config/mcp-bank").expanduser()
SESSION_FILE = CONFIG_DIR / "session.json"
DB_FILE = CONFIG_DIR / "cache.db"
CHAT_LOG_FILE = CONFIG_DIR / "chat.log"
SYNC_LOG_FILE = CONFIG_DIR / "sync.log"

# Every file here can hold sensitive data — Enable Banking session tokens,
# provider API keys, or (in the case of the logs) redacted-but-still-worth-
# protecting chat/sync activity. Locked to owner-only read/write.
_SECURE_FILE_MODE = 0o600
SECURE_FILES = (DB_FILE, SESSION_FILE, CHAT_LOG_FILE, SYNC_LOG_FILE, ENV_FILE)

# Shared rotation policy for chat.log and sync.log alike — 1 MiB per file,
# 3 rotated backups kept (.1/.2/.3).
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3


class _SecureRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that chmods 0600 every time it (re)opens its base
    file — including right after a rollover reopens it — so a freshly
    created or recreated log file is never briefly world/group readable.
    Rotated backups (.1/.2/.3) end up 0600 too: doRollover() renames the
    already-0600 base file into place, and os.rename() preserves mode."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, _SECURE_FILE_MODE)
        except OSError:
            pass
        return stream


def secure_rotating_handler(path: Path, fmt: str, datefmt: str | None = None) -> _SecureRotatingFileHandler:
    """Build a size-rotated (LOG_MAX_BYTES x LOG_BACKUP_COUNT), owner-only
    file handler for `path`. Shared by chat.log (web/app.py) and sync.log
    (kernel/sync.py, jobs/tips.py, jobs/evals.py) so both get identical
    rotation + permission handling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _SecureRotatingFileHandler(
        str(path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    return handler


def secure_config_files() -> None:
    """chmod 0600 every sensitive config file that currently exists.
    Idempotent and best-effort — safe to call unconditionally on every
    process startup (web app, scheduler) and every job entry (run_sync,
    run_tips, run_evals): skips files that don't exist yet, swallows
    permission errors rather than crashing startup."""
    for path in SECURE_FILES:
        try:
            if path.exists():
                os.chmod(path, _SECURE_FILE_MODE)
        except OSError:
            pass
