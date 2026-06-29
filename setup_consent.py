import uuid, requests
from datetime import datetime, timezone, timedelta
from pprint import pprint
from lib.auth import auth_headers, BASE_URL, REDIRECT_URL
from lib.storage import Storage

storage = Storage()

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

r = requests.post(f"{BASE_URL}/auth", json=body, headers=auth_headers())
print(r.status_code, r.json())
auth_url = r.json()["url"]
print("\nOpen this URL in your browser and complete MitID:")
print(auth_url)

# Step 2 — paste code from callback page
code = input("\nPaste the code from the callback page: ").strip()

# Step 3 — exchange code for session
r = requests.post(f"{BASE_URL}/sessions", json={"code": code}, headers=auth_headers())
print(r.status_code)
session = r.json()
print("\nAccounts:")
pprint(session.get("accounts", []))

# Step 4 — save to disk
storage.save_session({
    "session_id": session["session_id"],
    "accounts": session["accounts"],
    "valid_until": body["access"]["valid_until"],
})
print("\nSaved session.")