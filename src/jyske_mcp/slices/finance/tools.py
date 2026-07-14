# This file must never call the Enable Banking API directly.
# All data comes from SQLite. See jyske_mcp/kernel/sync.py for data fetching.
#
# The 23 finance tool implementations, as plain callables. Relocated out of
# jyske_mcp/mcp/server.py at epic deliverable #7a
# (.agent/epics/vsa-restructure-blueprint.md §4) — behavior-preserving move,
# no logic changes. The FastMCP server these were registered on (and the
# standalone `python -m jyske_mcp.mcp.server` Claude Desktop entrypoint) was
# retired at epic deliverable #8 (§6) — the PWA chat loop is the interface
# now; jyske_mcp/slices/finance/registry.py pairs each function below with
# its Anthropic-shaped schema.

import json
from datetime import datetime, timezone, timedelta

from jyske_mcp.slices.finance.storage import Storage
from jyske_mcp.kernel.storage import SessionExpiredError
from jyske_mcp.kernel.categorizer import categorize, top_categories, category_tree, validate_category_pair
from jyske_mcp.kernel.dto import AccountDTO, BalanceSnapshotDTO
from jyske_mcp.slices.finance.spending import (
    _month_bounds,
    _prev_month,
    _compute_proration,
    _parse_iso_date,
)
from jyske_mcp.slices.finance.recurring import _classify_recurring

storage = Storage()


def _validate_category(category: str) -> str | None:
    """None if category is empty/valid, else a one-line error string naming
    the bad value. Call at the top of any tool taking a `category` param."""
    if not category:
        return None
    valid = top_categories()
    if category not in valid:
        return f"Unknown category {category!r}. Valid categories: {', '.join(sorted(valid))}."
    return None


# ── tools ────────────────────────────────────────────────────────────────────

def list_accounts() -> str:
    """List all bank accounts from the active consent session."""
    try:
        session = storage.get_session()
    except SessionExpiredError as e:
        return str(e)

    raw_accounts = session.get("accounts", [])
    if not raw_accounts:
        return "No accounts found in session."

    lines = []
    for raw in raw_accounts:
        acc = AccountDTO.from_raw(raw)
        iban = acc.iban if acc.iban is not None else "unknown"
        product = acc.product if acc.product is not None else ""
        currency = acc.currency if acc.currency is not None else ""
        uid = raw["uid"]
        lines.append(f"{product} ({currency})  IBAN: {iban}  uid: {uid}")

    return "\n".join(lines)


def get_balances(account_uid: str = "") -> str:
    """
    Get balances for one or all accounts from local cache.
    Leave account_uid empty to fetch all accounts.
    """
    try:
        session = storage.get_session()
    except SessionExpiredError as e:
        return str(e)

    raw_accounts = session.get("accounts", [])
    if account_uid:
        raw_accounts = [a for a in raw_accounts if a["uid"] == account_uid]
        if not raw_accounts:
            return f"No account with uid {account_uid!r} found in session."

    lines = []
    for raw in raw_accounts:
        uid = raw["uid"]
        acc = AccountDTO.from_raw(raw)
        iban = acc.iban if acc.iban is not None else uid
        product = acc.product if acc.product is not None else ""

        data = storage.get_balances_cached(uid)
        if data is None:
            lines.append(f"{product} — {iban}: no balance data cached yet. Run a sync first.")
            continue

        lines.append(f"{product} — {iban}:")
        snapshot = BalanceSnapshotDTO.from_raw(uid, data, storage.balance_fetched_at(uid))
        for b in snapshot.balances:
            balance_type = b.balance_type if b.balance_type is not None else "balance"
            amount = b.amount if b.amount is not None else "?"
            currency = b.currency if b.currency is not None else ""
            lines.append(f"  {balance_type:25s}  {amount:>12}  {currency}")

    return "\n".join(lines) if lines else "No balance data returned."


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
        cat_str = f"{cat.category_top} > {cat.category_mid}" if cat else "[needs_categorization]"

        lines.append(f"  {date}  {amount_str}  {raw_name:<35}  {cat_str}")

    return "\n".join(lines)


def categorize_transaction(
    raw_name: str,
    mcc: str | None = None,
    llm_category: str | None = None,
) -> str:
    """
    Categorize a merchant by name and optional MCC code.

    Two-step flow:
      - Call without llm_category: tries the merchant cache.
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
        # Interactive chat-tool call — the calling Claude can retry in the
        # same turn, so give it feedback instead of silently coercing (unlike
        # the unattended nightly-sync path in kernel/sync.py, which coerces).
        top_valid, mid_valid = validate_category_pair(top, mid)
        if not top_valid:
            return _validate_category(top)
        if not mid_valid:
            valid_mids = ", ".join(category_tree()[top])
            return f"Unknown category_mid {mid!r} for {top!r}. Valid mids: {valid_mids}."
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

    top  = result.category_top
    mid  = result.category_mid
    leaf = result.category_leaf
    src  = result.source
    return f"{top} > {mid} > {leaf}  (source={src})"


def get_sync_status() -> str:
    """Returns when data was last synced. Call this as part of every opening brief."""
    last = storage.get_last_sync()
    if last is None:
        return (
            "No sync has been run yet. "
            "Run 'python jyske_mcp/jobs/sync.py' or start jyske_mcp/jobs/scheduler.py to populate data."
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


def get_memory() -> str:
    """
    Always call this at the start of every session.
    Returns the user profile (goals, preferences, known patterns)
    and the last 3 session summaries in a compact format.
    """
    profile_data = {}
    for k in ["preferences", "patterns", "pending"]:
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


def update_memory(session_summary: str, profile_updates: str | None = None) -> str:
    """
    Always call this at the end of every session.
    session_summary: 2-3 sentence plain language summary of what happened this session.
    profile_updates: JSON string of profile keys to update. Valid keys:
      - 'preferences': how user likes data presented, language preference, categories they care about
      - 'patterns': recurring behaviors or anomalies worth remembering long-term
      - 'pending': things flagged but not resolved, awaiting follow-up next session
    Goals are no longer stored here — use set_goal / update_goal_progress.
    Only include keys that actually changed this session.
    """
    storage.add_session_summary(session_summary)

    updated = []
    if profile_updates is not None:
        try:
            updates = json.loads(profile_updates)
        except json.JSONDecodeError as e:
            return f"profile_updates is not valid JSON: {e}"

        valid_keys = {"preferences", "patterns", "pending"}
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


def set_budget(category: str, limit_amount: float, period: str = "monthly") -> str:
    """Set a spending budget. category must be a top-level category from data/categories.json."""
    storage.set_budget(category_top=category, limit_amount=limit_amount, period=period)
    return f"Budget set: {category} — {limit_amount:.2f} / {period}."


def get_budget_status() -> str:
    """
    Get current budget status. Always call this as part of the opening brief.
    spent, percent, and status are DKK only. If other_currency_amounts is
    present, the category also has non-DKK spend not reflected in
    spent/percent; report it separately, never add it in.
    """
    rows = storage.get_budget_status()
    if not rows:
        return "No budgets set. Use set_budget to create one."
    return json.dumps(rows)


def get_goals() -> str:
    """Get all active goals with progress."""
    goals = storage.get_goals(agent_id="finance")
    if not goals:
        return "No active goals."
    return json.dumps([g.model_dump() for g in goals])


def set_goal(name: str, target_amount: float, purpose: str, deadline: str) -> str:
    """Create a new savings or spending goal."""
    goal_id = storage.set_goal(
        agent_id="finance",
        name=name,
        target_amount=target_amount,
        purpose=purpose,
        deadline=deadline,
    )
    return f"Goal created (id={goal_id}): {name} — {target_amount:.2f} by {deadline}."


def update_goal_progress(goal_id: int, current_amount: float) -> str:
    """Update progress on a goal."""
    storage.update_goal_progress(goal_id, current_amount)
    return f"Goal {goal_id} progress updated to {current_amount:.2f}."


def get_onboarding_status() -> str:
    """Check if budget onboarding is complete. Returns current stage if not."""
    status = storage.get_onboarding(agent_id="finance")
    if status is None:
        return json.dumps({"complete": False, "stage": "income"})
    if status.completed_at:
        return json.dumps({"complete": True})
    return json.dumps({"complete": False, **status.model_dump()})


def set_onboarding_stage(
    stage: str,
    income: float | None = None,
    income_day: int | None = None,
    fixed_costs: str | None = None,
    savings_monthly: float | None = None,
    savings_purpose: str | None = None,
    savings_target: float | None = None,
    savings_deadline: str | None = None,
    budget_style: str | None = None,
) -> str:
    """Record progress through budget onboarding. Call once per stage as the user answers."""
    fields = {
        "income": income,
        "income_day": income_day,
        "fixed_costs": fixed_costs,
        "savings_monthly": savings_monthly,
        "savings_purpose": savings_purpose,
        "savings_target": savings_target,
        "savings_deadline": savings_deadline,
        "budget_style": budget_style,
    }
    kwargs = {k: v for k, v in fields.items() if v is not None}
    storage.set_onboarding_stage(agent_id="finance", stage=stage, **kwargs)
    return f"Onboarding stage set: {stage}."


def complete_onboarding() -> str:
    """Mark budget onboarding as complete."""
    storage.complete_onboarding(agent_id="finance")
    return "Onboarding complete."


def get_overspend_patterns() -> str:
    """Returns categories overspent 3+ consecutive months. Call monthly."""
    patterns = storage.get_overspend_patterns(agent_id="finance", consecutive_months=3)
    if not patterns:
        return "No recurring overspend patterns detected."
    return json.dumps([p.model_dump() for p in patterns])


# ── deterministic math / aggregation tools ──────────────────────────────────
# These exist so the chat LLM never has to sum or compare a transaction
# listing by hand — all arithmetic happens here in Python against the SQLite
# cache, and the tool just returns the finished numbers as JSON.

def get_spending(
    date_from: str = "",
    date_to: str = "",
    category: str = "",
    group_by: str = "category",
    account_uid: str = "",
) -> str:
    """
    Sum spending (debits only, direction != CRDT) between two ISO dates.
    Defaults date_from/date_to to the current calendar month if left empty.
    group_by: "category" (default), "mid", "month", or "none".
    category, if given, must be a top-level category name and narrows the sum
    to that category only.
    total is a map of currency -> amount (e.g. {"DKK": 500.0}), never a
    single number — no currency conversion. Report each currency separately.
    """
    err = _validate_category(category)
    if err:
        return err

    now = datetime.now(timezone.utc)
    if not date_from:
        date_from = now.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = now.strftime("%Y-%m-%d")

    try:
        rows = storage.sum_spending(
            date_from=date_from,
            date_to=date_to,
            category_top=category or None,
            account_uid=account_uid or None,
            group_by=group_by,
        )
    except ValueError as e:
        return str(e)

    # Never blend currencies into a single number — fold per currency instead
    # (today every account is DKK, but this stops a future non-DKK account
    # from silently corrupting the total; no exchange rate is applied).
    total: dict[str, float] = {}
    for r in rows:
        total[r["currency"]] = round(total.get(r["currency"], 0.0) + r["amount"], 2)
    count = sum(r["count"] for r in rows)
    return json.dumps({
        "date_from": date_from,
        "date_to":   date_to,
        "group_by":  group_by,
        "total":     total,
        "count":     count,
        "breakdown": rows,
    })


def compare_spending(month: str = "", baseline_month: str = "", category: str = "") -> str:
    """
    Compare total spending in `month` against `baseline_month` (both "YYYY-MM").
    Defaults month to the current calendar month and baseline_month to the
    month immediately before it. If `category` is given (top-level category),
    narrows to that category and breaks down by mid-category instead.
    totals is keyed by currency; each currency has its own current/baseline/
    delta/pct_change (+ baseline_prorated/low_confidence for an in-progress
    month). Never combine currencies.
    """
    err = _validate_category(category)
    if err:
        return err

    now = datetime.now(timezone.utc)
    if not month:
        month = now.strftime("%Y-%m")
    if not baseline_month:
        baseline_month = _prev_month(month)

    group_by = "mid" if category else "category"

    date_from, date_to = _month_bounds(month)
    baseline_from, baseline_to = _month_bounds(baseline_month)

    current_rows = storage.sum_spending(
        date_from, date_to, category_top=category or None, group_by=group_by
    )
    baseline_rows = storage.sum_spending(
        baseline_from, baseline_to, category_top=category or None, group_by=group_by
    )

    def _index(rows: list[dict]) -> dict[tuple, float]:
        return {(r["key"], r["currency"]): r["amount"] for r in rows}

    cur_idx = _index(current_rows)
    base_idx = _index(baseline_rows)

    # `month` is still in progress (i.e. it's the current calendar month and
    # today isn't its last day) — comparing it as-is against a FULL prior
    # calendar month understates "current" and overstates any drop. Prorate
    # the baseline to the same day-of-month so the comparison is apples to
    # apples, and flag low_confidence when very little of the month has
    # elapsed. This only applies to the current-month case; two completed
    # past months are compared exactly as before.
    proration = _compute_proration(month, baseline_month, now)
    in_progress = proration["in_progress"]
    low_confidence = proration["low_confidence"]
    base_prorated_idx: dict[tuple, float] = {}
    if in_progress:
        baseline_prorated_rows = storage.sum_spending(
            proration["baseline_prorated_from"], proration["baseline_prorated_to"],
            category_top=category or None, group_by=group_by,
        )
        base_prorated_idx = _index(baseline_prorated_rows)

    breakdown = []
    for key, currency in set(cur_idx) | set(base_idx):
        current = cur_idx.get((key, currency), 0.0)
        baseline = base_idx.get((key, currency), 0.0)
        delta = round(current - baseline, 2)
        pct_change = round(delta / baseline * 100, 1) if baseline else None
        # None means "not yet categorized" — distinct from the literal "Other"
        # top-level category — so don't collapse the two into one label.
        entry = {
            "category":   key if key is not None else "Uncategorized",
            "currency":   currency,
            "current":    round(current, 2),
            "baseline":   round(baseline, 2),
            "delta":      delta,
            "pct_change": pct_change,
        }
        if in_progress:
            entry["baseline_prorated"] = round(base_prorated_idx.get((key, currency), 0.0), 2)
            entry["low_confidence"] = low_confidence
        breakdown.append(entry)
    breakdown.sort(key=lambda r: abs(r["delta"]), reverse=True)

    # Never blend currencies into a single total — fold per currency instead
    # (no exchange rate is applied).
    totals: dict[str, dict] = {}
    for r in breakdown:
        t = totals.setdefault(r["currency"], {"current": 0.0, "baseline": 0.0})
        t["current"]  = round(t["current"]  + r["current"], 2)
        t["baseline"] = round(t["baseline"] + r["baseline"], 2)
        if in_progress:
            t["baseline_prorated"] = round(t.get("baseline_prorated", 0.0) + r["baseline_prorated"], 2)
    for t in totals.values():
        t["delta"]      = round(t["current"] - t["baseline"], 2)
        t["pct_change"] = round(t["delta"] / t["baseline"] * 100, 1) if t["baseline"] else None
        if in_progress:
            t["low_confidence"] = low_confidence

    return json.dumps({
        "month":          month,
        "baseline_month": baseline_month,
        "group_by":       group_by,
        "totals":         totals,
        "breakdown":      breakdown,
    })


def goal_pace(goal_id: int = 0) -> str:
    """
    Compute pacing math for active goals: percent complete, whether on track
    for the deadline, and the daily/monthly amount required to still hit it.
    goal_id = 0 (default) means all active goals.
    """
    goals = storage.get_goals(agent_id="finance")
    if goal_id:
        goals = [g for g in goals if g.id == goal_id]

    now = datetime.now(timezone.utc)
    results = []
    for g in goals:
        target = g.target_amount or 0.0
        current = g.current_amount or 0.0
        deadline_date = _parse_iso_date(g.deadline)
        created_date = (
            datetime.fromtimestamp(g.created_at, tz=timezone.utc)
            if g.created_at else None
        )

        pct_complete = round(current / target * 100, 1) if target > 0 else None

        days_total = None
        days_elapsed = None
        days_remaining = None
        expected_now = None
        if created_date:
            days_elapsed = (now - created_date).days
            if deadline_date:
                days_total = (deadline_date - created_date).days
                days_remaining = (deadline_date - now).days
                if days_total > 0:
                    expected_now = target * days_elapsed / days_total
        elif deadline_date:
            days_remaining = (deadline_date - now).days

        if target > 0 and current >= target:
            status = "complete"
        elif days_remaining is not None and days_remaining <= 0:
            status = "overdue"
        elif expected_now is not None:
            # ±5% tolerance band around the expected-by-now amount
            tolerance = expected_now * 0.05
            if current > expected_now + tolerance:
                status = "ahead"
            elif current < expected_now - tolerance:
                status = "behind"
            else:
                status = "on_track"
        else:
            # deadline is free text or goal has no created_at — pacing math
            # can't be computed, only pct_complete stands on its own
            status = "unknown"

        amount_remaining = round(target - current, 2)
        required_daily = None
        required_monthly = None
        if days_remaining is not None and days_remaining > 0:
            required_daily = round(amount_remaining / days_remaining, 2)
            required_monthly = round(required_daily * 30.4, 2)

        projected_completion_date = None
        if days_elapsed and days_elapsed > 0 and current > 0 and target > 0 and created_date:
            daily_rate = current / days_elapsed
            if daily_rate > 0:
                days_to_target = target / daily_rate
                projected_completion_date = (
                    created_date + timedelta(days=days_to_target)
                ).strftime("%Y-%m-%d")

        results.append({
            "goal_id":                    g.id,
            "name":                       g.name,
            "status":                     status,
            "pct_complete":               pct_complete,
            "days_remaining":             days_remaining,
            "required_daily":             required_daily,
            "required_monthly":           required_monthly,
            "expected_now":               round(expected_now, 2) if expected_now is not None else None,
            "projected_completion_date":  projected_completion_date,
        })

    return json.dumps(results)


# ── recurring charges ────────────────────────────────────────────────────────
# Classification math itself lives in recurring.py; these tools only wire it
# up to Storage and the JSON-string tool-call boundary.

def recurring_charges(lookback_days: int = 180, min_count: int = 3) -> str:
    """
    Detect recurring/subscription-like charges and frequent merchants from
    transaction history. Classifies each qualifying merchant as
    "fixed_recurring" (stable price, regular cadence — a subscription,
    tolerant of a single price change) and/or "frequent_merchant" (high
    frequency or regular cadence but variable amount — groceries, usage-billed
    utilities). Flags merchants that have gone quiet (needs_confirmation) so
    the agent can ask the user whether they cancelled it.
    """
    candidates = storage.get_recurring_candidates(lookback_days=lookback_days, min_count=min_count)
    statuses = storage.get_recurring_statuses()
    today = datetime.now(timezone.utc)

    # _classify_recurring is a pure function tested directly with hand-built
    # plain dicts (tests/test_classify_recurring.py) — convert the DTOs from
    # Storage back to that same dict shape at this boundary rather than
    # changing its signature.
    candidate_dicts = [c.model_dump() for c in candidates]
    status_dicts = {k: v.model_dump() for k, v in statuses.items()}

    recurring = []
    for candidate in candidate_dicts:
        row = _classify_recurring(candidate, status_dicts, today, lookback_days, min_count)
        if row is not None:
            recurring.append(row)

    recurring.sort(key=lambda r: r["monthly_estimate"], reverse=True)

    currencies = {r["currency"] for r in recurring}
    currency_note = None
    if len(currencies) > 1:
        currency_note = (
            f"Multiple currencies present ({', '.join(sorted(currencies))}) — "
            "amounts are not converted, treat totals per currency separately."
        )

    return json.dumps({
        "lookback_days": lookback_days,
        "as_of":         today.strftime("%Y-%m-%d"),
        "currency_note": currency_note,
        "recurring":     recurring,
    })


def confirm_recurring_status(merchant: str, status: str, currency: str = "DKK") -> str:
    """
    Record the user's answer to a cancellation-confirmation question raised
    by recurring_charges (needs_confirmation: true). status must be one of
    "active", "cancelled", "unknown".
    """
    valid_statuses = {"active", "cancelled", "unknown"}
    if status not in valid_statuses:
        return f"Invalid status {status!r}. Must be one of {sorted(valid_statuses)}."

    storage.set_recurring_status(merchant, currency, status)
    return json.dumps({"merchant": merchant, "status": status, "recorded": True})


# ── tip of the day ───────────────────────────────────────────────────────────
# Tips are generated nightly by jyske_mcp/slices/finance/tips.py and persisted
# via Storage.create_tip; these tools only ever read/update that row, never
# call an LLM or Enable Banking themselves.

_TIP_VERDICTS = {"accepted", "rejected"}
_TIP_REASON_CODES = {
    "not_representative", "already_addressed", "not_actionable",
    "inaccurate", "not_relevant", "other",
}


def get_current_tip() -> str:
    """
    Returns today's financial tip of the day, if one was generated overnight.
    Call this opportunistically — as part of the opening brief, or whenever
    the user's message could plausibly be reacting to a tip.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tip = storage.get_tip_for_date(today)
    if tip is None:
        return "No tip generated today."
    return json.dumps({
        "tip_id":          tip.id,
        "tip_date":        tip.tip_date,
        "tip_text":        tip.tip_text,
        "feedback_status": tip.feedback_status,
    })


def submit_tip_feedback(tip_id: int, verdict: str, reason_text: str, reason_code: str = "") -> str:
    """
    Record the user's conversational (chat) reaction to a tip. verdict must
    be 'accepted' or 'rejected' — always call this with an explicit verdict
    when the user pushes back on or endorses a tip, never silently move on.
    reason_text is required: capture the user's actual words/reasoning, not
    just the verdict. reason_code is an optional classifier, one of:
    not_representative, already_addressed, not_actionable, inaccurate,
    not_relevant, other.
    """
    if verdict not in _TIP_VERDICTS:
        return f"Invalid verdict {verdict!r}. Must be one of {sorted(_TIP_VERDICTS)}."
    if not reason_text or not reason_text.strip():
        return "reason_text is required — capture why the user accepted or rejected the tip."
    if reason_code and reason_code not in _TIP_REASON_CODES:
        return f"Invalid reason_code {reason_code!r}. Must be one of {sorted(_TIP_REASON_CODES)}."

    try:
        storage.set_tip_feedback(
            tip_id, verdict, reason_code or None, reason_text, source="chat"
        )
    except ValueError as e:
        return str(e)

    return f"Feedback recorded: tip {tip_id} — {verdict}."
