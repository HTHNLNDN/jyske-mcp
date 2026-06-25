import jwt, time, uuid, requests, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pprint import pprint
from dotenv import load_dotenv
import os

load_dotenv()

APP_ID = os.environ["ENABLE_BANKING_APP_ID"]
PRIVATE_KEY = Path(os.environ["ENABLE_BANKING_PRIVATE_KEY_PATH"]).expanduser().read_text()
BASE_URL = "https://api.enablebanking.com"
REDIRECT_URL = os.environ["ENABLE_BANKING_REDIRECT_URL"]
SESSION_FILE = Path("~/.config/mcp-bank/session.json").expanduser()

def make_token():
    now = int(time.time())
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + 3600,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256", headers={"kid": APP_ID})

def headers():
    return {"Authorization": f"Bearer {make_token()}"}

# Step 1 — start auth
body = {
    "access": {
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=180)).isoformat()
    },
    "aspsp": {
        "name": "Jyske Bank",
        "country": "DK"
    },
    "state": str(uuid.uuid4()),
    "redirect_url": REDIRECT_URL,
    "psu_type": "personal",
}

r = requests.post(f"{BASE_URL}/auth", json=body, headers=headers())
print(r.status_code, r.json())
auth_url = r.json()["url"]
print("\nOpen this URL in your browser and complete MitID:")
print(auth_url)

# Step 2 — paste code from callback page
code = input("\nPaste the code from the callback page: ").strip()

# Step 3 — exchange code for session
r = requests.post(f"{BASE_URL}/sessions", json={"code": code}, headers=headers())
print(r.status_code)
session = r.json()
print("\nAccounts:")
pprint(session.get("accounts", []))

# Step 4 — save to disk
SESSION_FILE.write_text(json.dumps({
    "session_id": session["session_id"],
    "accounts": session["accounts"],
    "valid_until": body["access"]["valid_until"],
}, indent=2))
print(f"\nSaved to {SESSION_FILE}")