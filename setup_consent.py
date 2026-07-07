"""Terminal fallback for (re-)establishing an Enable Banking consent session.
Prefer the in-app flow (Settings › Bank connection › Reconnect, via
app.py's /consent/* routes) — this script shares the same lib.consent logic
so the two paths can't drift apart.
"""
from pprint import pprint

from lib.consent import start_authorization, complete_authorization
from lib.storage import Storage

storage = Storage()

# Step 1 — start auth
result = start_authorization(storage)
print("\nOpen this URL in your browser and complete MitID:")
print(result["auth_url"])

# Step 2 — paste code from callback page/URL
code = input("\nPaste the code from the callback: ").strip()

# Step 3 — exchange code for session (also reconciles account uids against
# any prior session, and saves the new session to disk)
outcome = complete_authorization(storage, code, result["state"])

print("\nAccounts:")
pprint(outcome["accounts"])
if outcome["remapped"]:
    print("\nRemapped account uids (cached transactions/balances carried over):")
    pprint(outcome["remapped"])
print("\nSaved session.")
