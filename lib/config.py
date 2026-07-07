from pathlib import Path

CONFIG_DIR = Path("~/.config/mcp-bank").expanduser()
SESSION_FILE = CONFIG_DIR / "session.json"
DB_FILE = CONFIG_DIR / "cache.db"
