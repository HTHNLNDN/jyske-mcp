# This file must never call the Enable Banking API directly.
# All data comes from SQLite. See cron/sync.py for data fetching.

import json
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP
from lib.storage import Storage, SessionExpiredError
from lib.categorizer import categorize

mcp = FastMCP("jyske-bank")
storage = Storage()


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_accounts() -> str:
    """List all bank accounts from the active consent session."""
    try:
        session = storage.get_session()
    except SessionExpiredError as e:
        return str(e)

    accounts = session.get("accounts", [])
    if not accounts:
        return "No accounts found in session."

    lines = []
    for acc in accounts:
        iban = acc.get("account_id", {}).get("iban", "unknown")
        product = acc.get("product", "")
        currency = acc.get("currency", "")
        uid = acc["uid"]
        lines.append(f"{product} ({currency})  IBAN: {iban}  uid: {uid}")

    return "\n".join(lines)


@mcp.tool()
def get_balances(account_uid: str = "") -> str:
    """
    Get balances for one or all accounts from local cache.
    Leave account_uid empty to fetch all accounts.
    """
    try:
        session = storage.get_session()
    except SessionExpiredError as e:
        return str(e)

    accounts = session.get("accounts", [])
    if account_uid:
        accounts = [a for a in accounts if a["uid"] == account_uid]
        if not accounts:
            return f"No account with uid {account_uid!r} found in session."

    lines = []
    for acc in accounts:
        uid = acc["uid"]
        iban = acc.get("account_id", {}).get("iban", uid)
        product = acc.get("product", "")

        data = storage.get_balances_cached(uid)
        if data is None:
            lines.append(f"{product} — {iban}: no balance data cached yet. Run a sync first.")
            continue

        lines.append(f"{product} — {iban}:")
        for b in data.get("balances", []):
            amt = b.get("balance_amount", {})
            lines.append(
                f"  {b.get('balance_type', 'balance'):25s}"
                f"  {amt.get('amount', '?'):>12}  {amt.get('currency', '')}"
            )

    return "\n".join(lines) if lines else "No balance data returned."


@mcp.tool()
def get_transactions(
    account_uid: str,
    date_from: str = "",
    date_to: str = "",
) -> str:
    """
    Get transactions for an account from local cache.
    date_from and date_to are optional ISO dates (YYYY-MM-DD); defaults to last 30 days.
    """
    if not date_from:
        date_from = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    transactions = storage.get_transactions_cached(account_uid, date_from, date_to)

    if not transactions:
        return (
            f"No cached transactions for this account in {date_from} → {date_to}. "
            "Run a sync if data is missing."
        )

    lines = [f"Transactions {date_from} → {date_to}  (account: {account_uid})"]
    for t in transactions:
        date = t.get("booking_date") or t.get("value_date", "?")
        amt = t.get("transaction_amount", {})
        direction = t.get("credit_debit_indicator", "")
        sign = "+" if direction == "CRDT" else "-"
        amount_str = f"{sign}{amt.get('amount', '?'):>10} {amt.get('currency', '')}"
        raw_name = (
            t.get("creditor_name")
            or (t.get("remittance_information") or [""])[0]
            or t.get("debtor_name", "")
        )
        mcc = t.get("mcc") or t.get("merchant_category_code")

        cat = categorize(raw_name, mcc, storage)
        cat_str = f"{cat['category_top']} > {cat['category_mid']}" if cat else "[needs_categorization]"

        lines.append(f"  {date}  {amount_str}  {raw_name:<35}  {cat_str}")

    return "\n".join(lines)


@mcp.tool()
def categorize_transaction(
    raw_name: str,
    mcc: str | None = None,
    llm_category: str | None = None,
) -> str:
    """
    Categorize a merchant by name and optional MCC code.

    Two-step flow:
      - Call without llm_category: tries merchant cache then MCC lookup.
        Returns the category on hit, or {"needs_llm": true, "raw_name": ...}
        to signal that Claude should determine the category and call again.
      - Call with llm_category (format "Top > Mid > Leaf"): stores the
        LLM-derived category and returns it.
    """
    if llm_category is not None:
        parts = [p.strip() for p in llm_category.split(">")]
        if len(parts) != 3:
            return "llm_category must be in format 'Top > Mid > Leaf'"
        top, mid, leaf = parts
        storage.merchant_set(
            raw_name=raw_name,
            category_top=top,
            category_mid=mid,
            category_leaf=leaf,
            mcc=mcc or "",
            source="llm",
        )
        return f"{top} > {mid} > {leaf}  (stored, source=llm)"

    result = categorize(raw_name, mcc, storage)
    if result is None:
        return json.dumps({"needs_llm": True, "raw_name": raw_name, "mcc": mcc})

    top  = result["category_top"]
    mid  = result["category_mid"]
    leaf = result["category_leaf"]
    src  = result["source"]
    return f"{top} > {mid} > {leaf}  (source={src})"


@mcp.tool()
def get_sync_status() -> str:
    """Returns when data was last synced. Call this as part of every opening brief."""
    last = storage.get_last_sync()
    if last is None:
        return (
            "No sync has been run yet. "
            "Run 'python cron/sync.py' or start cron/scheduler.py to populate data."
        )

    completed = datetime.fromtimestamp(last["completed_at"], tz=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - completed).total_seconds()
    age_hours = age_seconds / 3600

    if age_hours < 48:
        age_str = f"{age_hours:.1f}h ago"
    else:
        age_str = f"{age_hours / 24:.0f} days ago"

    lines = [
        f"Last sync: {completed.strftime('%Y-%m-%d %H:%M')} UTC ({age_str})",
        f"Accounts: {last['accounts_synced']}  |  "
        f"Fetched: {last['transactions_fetched']}  |  "
        f"New: {last['new_transactions']}",
    ]

    if last.get("errors"):
        try:
            details = json.loads(last["errors"])
            errs = details.get("errors", [])
            if errs:
                lines.append(f"Sync errors: {'; '.join(errs)}")
        except (json.JSONDecodeError, TypeError):
            pass

    if age_hours > 24:
        lines.append(f"WARNING: data is {age_str} old.")

    return "\n".join(lines)


@mcp.tool()
def get_memory() -> str:
    """
    Always call this at the start of every session.
    Returns the user profile (goals, preferences, known patterns)
    and the last 3 session summaries in a compact format.
    """
    profile_data = {}
    for k in ["goals", "preferences", "patterns", "pending"]:
        val = storage.get_profile(k)
        if val is not None:
            profile_data[k] = val

    lines = []
    if profile_data:
        lines.append("=== Profile ===")
        for key, val in profile_data.items():
            lines.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
    else:
        lines.append("No profile data yet.")

    summaries = storage.get_recent_summaries(3)
    if summaries:
        lines.append("\n=== Recent sessions ===")
        for i, summary in enumerate(summaries, 1):
            lines.append(f"[{i}] {summary}")
    else:
        lines.append("\nNo previous sessions.")

    return "\n".join(lines)


@mcp.tool()
def update_memory(session_summary: str, profile_updates: str | None = None) -> str:
    """
    Always call this at the end of every session.
    session_summary: 2-3 sentence plain language summary of what happened this session.
    profile_updates: JSON string of profile keys to update. Valid keys:
      - 'goals': list of active goals with target, deadline, current progress
      - 'preferences': how user likes data presented, language preference, categories they care about
      - 'patterns': recurring behaviors or anomalies worth remembering long-term
      - 'pending': things flagged but not resolved, awaiting follow-up next session
    Only include keys that actually changed this session.
    """
    storage.add_session_summary(session_summary)

    updated = []
    if profile_updates is not None:
        try:
            updates = json.loads(profile_updates)
        except json.JSONDecodeError as e:
            return f"profile_updates is not valid JSON: {e}"

        valid_keys = {"goals", "preferences", "patterns", "pending"}
        invalid = set(updates.keys()) - valid_keys
        if invalid:
            return (
                f"Unknown profile keys: {', '.join(sorted(invalid))}. "
                f"Valid keys: {', '.join(sorted(valid_keys))}"
            )

        for key, value in updates.items():
            storage.set_profile(key, value)
            updated.append(key)

    parts = ["Session summary saved."]
    if updated:
        parts.append(f"Profile updated: {', '.join(updated)}.")
    return " ".join(parts)


@mcp.tool()
def set_budget(category: str, limit_amount: float, period: str = "monthly") -> str:
    """Set a spending budget. category must be a top-level category from data/categories.json."""
    storage.set_budget(category_top=category, limit_amount=limit_amount, period=period)
    return f"Budget set: {category} — {limit_amount:.2f} / {period}."


@mcp.tool()
def get_budget_status() -> str:
    """Get current budget status. Always call this as part of the opening brief."""
    rows = storage.get_budget_status()
    if not rows:
        return "No budgets set. Use set_budget to create one."
    return json.dumps(rows)


if __name__ == "__main__":
    mcp.run()
