from pathlib import Path

# Repo root. Valid because this package is installed editable (pip install -e .)
# by design — alembic.ini, static/, and .env are repo files, not package data,
# and everything that needs them anchors here instead of counting parents.
ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"

CONFIG_DIR = Path("~/.config/mcp-bank").expanduser()
SESSION_FILE = CONFIG_DIR / "session.json"
DB_FILE = CONFIG_DIR / "cache.db"
