"""Terminal fallback for (re-)establishing an Enable Banking consent session.
Prefer the in-app flow (Settings › Bank connection › Reconnect, via the
/consent/* routes in jyske_mcp/web/app.py) — this script shares the same
jyske_mcp.consent logic so the two paths can't drift apart.

scripts/callback.html is the optional landing page for this flow: host it
anywhere and register its URL as the Enable Banking redirect URL, and it
displays the ?code= to paste below.
"""
from pprint import pprint

from jyske_mcp.consent import start_authorization, complete_authorization
from jyske_mcp.storage import Storage

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
