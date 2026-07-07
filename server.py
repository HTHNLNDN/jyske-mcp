# This file must never call the Enable Banking API directly.
# All data comes from SQLite. See cron/sync.py for data fetching.

import calendar
import json
import statistics
from collections import Counter
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP
from lib.storage import Storage, SessionExpiredError
from lib.categorizer import categorize, top_categories

mcp = FastMCP("jyske-bank")
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


@mcp.tool()
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


@mcp.tool()
def get_goals() -> str:
    """Get all active goals with progress."""
    goals = storage.get_goals(agent_id="finance")
    if not goals:
        return "No active goals."
    return json.dumps(goals)


@mcp.tool()
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


@mcp.tool()
def update_goal_progress(goal_id: int, current_amount: float) -> str:
    """Update progress on a goal."""
    storage.update_goal_progress(goal_id, current_amount)
    return f"Goal {goal_id} progress updated to {current_amount:.2f}."


@mcp.tool()
def get_onboarding_status() -> str:
    """Check if budget onboarding is complete. Returns current stage if not."""
    status = storage.get_onboarding(agent_id="finance")
    if status is None:
        return json.dumps({"complete": False, "stage": "income"})
    if status.get("completed_at"):
        return json.dumps({"complete": True})
    return json.dumps({"complete": False, **status})


@mcp.tool()
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


@mcp.tool()
def complete_onboarding() -> str:
    """Mark budget onboarding as complete."""
    storage.complete_onboarding(agent_id="finance")
    return "Onboarding complete."


@mcp.tool()
def get_overspend_patterns() -> str:
    """Returns categories overspent 3+ consecutive months. Call monthly."""
    patterns = storage.get_overspend_patterns(agent_id="finance", consecutive_months=3)
    if not patterns:
        return "No recurring overspend patterns detected."
    return json.dumps(patterns)


# ── deterministic math / aggregation tools ──────────────────────────────────
# These exist so the chat LLM never has to sum or compare a transaction
# listing by hand — all arithmetic happens here in Python against the SQLite
# cache, and the tool just returns the finished numbers as JSON.

def _month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> (first day, last day) as ISO date strings."""
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _prev_month(month: str) -> str:
    """'YYYY-MM' -> the previous calendar month, same string format."""
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{mon - 1:02d}"


@mcp.tool()
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

    total = round(sum(r["amount"] for r in rows), 2)
    count = sum(r["count"] for r in rows)
    return json.dumps({
        "date_from": date_from,
        "date_to":   date_to,
        "group_by":  group_by,
        "total":     total,
        "count":     count,
        "breakdown": rows,
    })


@mcp.tool()
def compare_spending(month: str = "", baseline_month: str = "", category: str = "") -> str:
    """
    Compare total spending in `month` against `baseline_month` (both "YYYY-MM").
    Defaults month to the current calendar month and baseline_month to the
    month immediately before it. If `category` is given (top-level category),
    narrows to that category and breaks down by mid-category instead.
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
    in_progress = month == now.strftime("%Y-%m")
    base_prorated_idx: dict[tuple, float] = {}
    low_confidence = False
    if in_progress:
        year, mon = int(month[:4]), int(month[5:7])
        days_in_month = calendar.monthrange(year, mon)[1]
        day_of_month = now.day
        pct_elapsed = day_of_month / days_in_month
        low_confidence = pct_elapsed < 0.25

        b_year, b_mon = int(baseline_month[:4]), int(baseline_month[5:7])
        baseline_last_day = calendar.monthrange(b_year, b_mon)[1]
        prorate_day = min(day_of_month, baseline_last_day)
        baseline_prorated_from = f"{baseline_month}-01"
        baseline_prorated_to = f"{baseline_month}-{prorate_day:02d}"

        baseline_prorated_rows = storage.sum_spending(
            baseline_prorated_from, baseline_prorated_to,
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

    total_current = round(sum(r["current"] for r in breakdown), 2)
    total_baseline = round(sum(r["baseline"] for r in breakdown), 2)
    total_delta = round(total_current - total_baseline, 2)
    total_pct_change = round(total_delta / total_baseline * 100, 1) if total_baseline else None

    totals = {
        "current":    total_current,
        "baseline":   total_baseline,
        "delta":      total_delta,
        "pct_change": total_pct_change,
    }
    if in_progress:
        totals["baseline_prorated"] = round(
            sum(r["baseline_prorated"] for r in breakdown), 2
        )
        totals["low_confidence"] = low_confidence

    return json.dumps({
        "month":          month,
        "baseline_month": baseline_month,
        "group_by":       group_by,
        "totals":         totals,
        "breakdown":      breakdown,
    })


def _parse_iso_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None  # deadline is free text, not an ISO date — pacing math can't run on it


@mcp.tool()
def goal_pace(goal_id: int = 0) -> str:
    """
    Compute pacing math for active goals: percent complete, whether on track
    for the deadline, and the daily/monthly amount required to still hit it.
    goal_id = 0 (default) means all active goals.
    """
    goals = storage.get_goals(agent_id="finance")
    if goal_id:
        goals = [g for g in goals if g["id"] == goal_id]

    now = datetime.now(timezone.utc)
    results = []
    for g in goals:
        target = g["target_amount"] or 0.0
        current = g["current_amount"] or 0.0
        deadline_date = _parse_iso_date(g.get("deadline"))
        created_date = (
            datetime.fromtimestamp(g["created_at"], tz=timezone.utc)
            if g.get("created_at") else None
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
            "goal_id":                    g["id"],
            "name":                       g["name"],
            "status":                     status,
            "pct_complete":               pct_complete,
            "days_remaining":             days_remaining,
            "required_daily":             required_daily,
            "required_monthly":           required_monthly,
            "expected_now":               round(expected_now, 2) if expected_now is not None else None,
            "projected_completion_date":  projected_completion_date,
        })

    return json.dumps(results)


# ── recurring charge classification ─────────────────────────────────────────
# The trickiest piece of this feature: distinguishing "fixed recurring"
# charges (same price, regular cadence — a subscription) from "frequent
# merchant" spend (regular cadence or high frequency, but a variable amount —
# groceries, usage-billed utilities) while tolerating a one-time price change
# on a subscription without breaking its classification. See _classify_recurring.

AMOUNT_TOLERANCE = 0.05          # 5% — how close two charge amounts must be to count as "the same price"
MIN_RUN_LEN = 2                  # a price must repeat at least this many times to count as "established"
GAP_REGULARITY_MAX_CV = 0.25     # max coefficient of variation (stdev/mean) of gaps to call cadence "regular"
FREQUENT_MIN_COUNT = 6           # charge count threshold for "frequent merchant" (independent of cadence)
FREQUENT_MAX_MEAN_GAP = 14       # average gap (days) threshold for "frequent merchant"
STALE_FACTOR = 2                 # a merchant is "stale" once the gap since last charge exceeds STALE_FACTOR * gap_median

# (bucket name, expected gap in days, ± tolerance in days)
_CADENCE_BUCKETS = [
    ("weekly",    7,   2),
    ("biweekly",  14,  3),
    ("monthly",   30,  6),
    ("quarterly", 91,  12),
    ("annual",    365, 30),
]


def _date_gaps(dates: list[str]) -> list[int]:
    parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    return [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]


def _match_cadence(gap_median: float | None) -> str | None:
    """Nearest cadence bucket whose tolerance band contains gap_median, or None."""
    if gap_median is None:
        return None
    candidates = [
        (name, abs(gap_median - center))
        for name, center, tol in _CADENCE_BUCKETS
        if abs(gap_median - center) <= tol
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[1])[0]


def _segment_runs(charges: list[tuple[str, float]]) -> list[dict]:
    """
    Walk charges chronologically, grouping consecutive charges into "runs" of
    a stable price. A charge joins the current run if it's within
    AMOUNT_TOLERANCE of that run's *running median* (recomputed on every
    append); otherwise it starts a new run. This is what lets a subscription
    price increase show up as two runs instead of destroying the whole
    history's "stability".
    """
    runs: list[dict] = []
    amounts: list[float] = []
    dates: list[str] = []
    for date, amount in charges:
        if not amounts:
            amounts, dates = [amount], [date]
            continue
        median = statistics.median(amounts)
        within_tolerance = (
            abs(amount - median) <= AMOUNT_TOLERANCE * abs(median)
            if median else amount == 0
        )
        if within_tolerance:
            amounts.append(amount)
            dates.append(date)
        else:
            runs.append({"amounts": amounts, "dates": dates})
            amounts, dates = [amount], [date]
    if amounts:
        runs.append({"amounts": amounts, "dates": dates})
    return runs


def _drop_interior_noise(runs: list[dict]) -> list[dict]:
    """Fold interior runs (bounded by a run on both sides) shorter than
    MIN_RUN_LEN into the preceding kept run — single stray charges that don't
    represent a real price change, just noise between two stretches of a
    stable price.

    These charges are *not* discarded: every charge must land in exactly one
    run. They're tracked in a separate "absorbed" list (date, amount) rather
    than merged into "amounts"/"dates" so a handful of outlier noise charges
    can't skew the run's median/typical price — only its count and date
    span."""
    if len(runs) < 3:
        return runs
    kept = [dict(runs[0])]
    for i in range(1, len(runs) - 1):
        if len(runs[i]["amounts"]) >= MIN_RUN_LEN:
            kept.append(dict(runs[i]))
        else:
            prev = kept[-1]
            prev["absorbed"] = prev.get("absorbed", []) + list(
                zip(runs[i]["dates"], runs[i]["amounts"])
            )
    kept.append(dict(runs[-1]))
    return kept


def _merge_adjacent_runs(runs: list[dict]) -> list[dict]:
    """After dropping interior noise, runs that are now adjacent may again be
    within tolerance of each other (the noise run in between was the only
    thing separating them) — merge those back together."""
    if not runs:
        return runs
    merged = [dict(runs[0])]
    for run in runs[1:]:
        prev = merged[-1]
        prev_median = statistics.median(prev["amounts"])
        run_median = statistics.median(run["amounts"])
        same_price = (
            abs(run_median - prev_median) <= AMOUNT_TOLERANCE * abs(prev_median)
            if prev_median else run_median == 0
        )
        if same_price:
            prev["amounts"] = prev["amounts"] + run["amounts"]
            prev["dates"] = prev["dates"] + run["dates"]
            prev["absorbed"] = prev.get("absorbed", []) + run.get("absorbed", [])
        else:
            merged.append(dict(run))
    return merged


def _run_charge_count(run: dict) -> int:
    """Total real charges represented by a run: the price-defining charges
    plus any interior-noise charges absorbed into it (see
    _drop_interior_noise). Always use this instead of len(run["amounts"])
    when reporting counts — the latter undercounts by the absorbed noise."""
    return len(run["amounts"]) + len(run.get("absorbed", []))


def _run_date_span(run: dict) -> tuple[str, str]:
    """(first_seen, last_seen) across a run's price-defining charges and any
    absorbed noise charges — computed via min/max rather than list
    positions, since absorbed dates aren't necessarily interleaved in
    chronological order within the run's date list."""
    all_dates = run["dates"] + [d for d, _ in run.get("absorbed", [])]
    return min(all_dates), max(all_dates)


def _segment_price_runs(charges: list[tuple[str, float]]) -> list[dict]:
    runs = _segment_runs(charges)
    runs = _drop_interior_noise(runs)
    runs = _merge_adjacent_runs(runs)
    return runs


def _established_runs(charges: list[tuple[str, float]]) -> tuple[list[dict], bool]:
    """
    Returns (established_runs, current_run_stable).

    established_runs are price-history runs oldest→newest, excluding a
    trailing run that's too short to "count" yet (a single new-looking charge
    that hasn't repeated — see MIN_RUN_LEN). The previous run is used as the
    "current" price in that case.

    current_run_stable is False only in the degenerate fallback case where
    segmentation couldn't find *any* run of length >= MIN_RUN_LEN — i.e. the
    amount genuinely varies charge to charge (groceries, usage-billed
    utilities) rather than settling on a price. True in every other case,
    including when a too-new trailing charge was excluded.
    """
    runs = _segment_price_runs(charges)
    if not runs:
        return [], False  # shouldn't happen — MIN_COUNT already passed upstream

    if len(runs[-1]["amounts"]) >= MIN_RUN_LEN:
        return runs, True

    established = runs[:-1]
    if established and len(established[-1]["amounts"]) >= MIN_RUN_LEN:
        return established, True

    # Degenerate fallback: collapse to one whole-history run so downstream
    # code always has something to read typical_amount/price_history off of,
    # but flag it unstable so fixed_recurring isn't (mis)claimed for data
    # that's actually just noisy/variable.
    all_amounts = [a for run in runs for a in run["amounts"]]
    all_dates = [d for run in runs for d in run["dates"]]
    all_absorbed = [pair for run in runs for pair in run.get("absorbed", [])]
    return [{"amounts": all_amounts, "dates": all_dates, "absorbed": all_absorbed}], False


def _classify_recurring(
    candidate: dict,
    statuses: dict[tuple[str, str], dict],
    today: datetime,
    lookback_days: int,
    min_count: int,
) -> dict | None:
    """Classify a single merchant candidate (see Storage.get_recurring_candidates).
    Returns None if the merchant is neither fixed_recurring nor frequent_merchant."""
    merchant = candidate["merchant"]
    currency = candidate["currency"]
    charges = candidate["charges"]  # [(date, amount), ...] sorted ascending
    count = len(charges)
    if count < min_count:
        return None

    dates = [d for d, _ in charges]
    gaps = _date_gaps(dates)
    if gaps:
        gap_mean = statistics.mean(gaps)
        gap_median = statistics.median(gaps)
        gap_stdev = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
        gap_cv = (gap_stdev / gap_mean) if gap_mean else None
    else:
        gap_mean = gap_median = gap_stdev = gap_cv = None

    cadence = _match_cadence(gap_median)
    cadence_regular = cadence is not None and gap_cv is not None and gap_cv <= GAP_REGULARITY_MAX_CV

    established, current_run_stable = _established_runs(charges)
    typical_amount = round(statistics.median(established[-1]["amounts"]), 2)
    price_history = []
    for run in established:
        first_seen_run, last_seen_run = _run_date_span(run)
        price_history.append({
            "amount":     round(statistics.median(run["amounts"]), 2),
            "first_seen": first_seen_run,
            "last_seen":  last_seen_run,
            "count":      _run_charge_count(run),
        })

    is_frequent = count >= FREQUENT_MIN_COUNT and gap_mean is not None and gap_mean <= FREQUENT_MAX_MEAN_GAP

    kinds = []
    if count >= min_count and cadence_regular and current_run_stable:
        kinds.append("fixed_recurring")
    if is_frequent or (cadence_regular and not current_run_stable and count >= min_count):
        kinds.append("frequent_merchant")
    if not kinds:
        return None
    primary_kind = "fixed_recurring" if "fixed_recurring" in kinds else "frequent_merchant"

    first_seen, last_seen = dates[0], dates[-1]
    last_seen_dt = datetime.strptime(last_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    first_seen_dt = datetime.strptime(first_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    stale = gap_median is not None and (today - last_seen_dt).days > STALE_FACTOR * gap_median

    conf = statuses.get((merchant, currency))
    covered = conf is not None and conf["confirmed_at"] >= last_seen_dt.timestamp()

    if conf and conf["status"] == "cancelled" and covered:
        status = "cancelled_confirmed"
    elif conf and conf["status"] == "active" and covered:
        status = "active"
    elif stale:
        status = "inactive_unconfirmed"
    else:
        status = "active"
    needs_confirmation = stale and not covered

    next_expected = None
    if cadence is not None and gap_median is not None:
        next_expected = (last_seen_dt + timedelta(days=gap_median)).strftime("%Y-%m-%d")

    total_in_window = round(sum(a for _, a in charges), 2)
    if cadence is not None and gap_median:
        monthly_estimate = round(typical_amount * (30 / gap_median), 2)
    else:
        span_days = (last_seen_dt - first_seen_dt).days
        denom_days = span_days if span_days > 0 else lookback_days
        monthly_estimate = round(total_in_window / (denom_days / 30), 2)

    cat_counts = Counter(c for c in candidate.get("categories", []) if c)
    category = cat_counts.most_common(1)[0][0] if cat_counts else "Other"

    return {
        "merchant":          merchant,
        "currency":          currency,
        "kinds":             kinds,
        "primary_kind":      primary_kind,
        "category":          category,
        "count":             count,
        "cadence":           cadence,
        "cadence_regular":   cadence_regular,
        "first_seen":        first_seen,
        "last_seen":         last_seen,
        "next_expected":     next_expected,
        "typical_amount":    typical_amount,
        "price_history":     price_history,
        "status":            status,
        "needs_confirmation": needs_confirmation,
        "monthly_estimate":  monthly_estimate,
        "total_in_window":   total_in_window,
    }


@mcp.tool()
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

    recurring = []
    for candidate in candidates:
        row = _classify_recurring(candidate, statuses, today, lookback_days, min_count)
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


@mcp.tool()
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
# Tips are generated nightly by cron/tips.py and persisted via
# Storage.create_tip; these tools only ever read/update that row, never call
# an LLM or Enable Banking themselves.

_TIP_VERDICTS = {"accepted", "rejected"}
_TIP_REASON_CODES = {
    "not_representative", "already_addressed", "not_actionable",
    "inaccurate", "not_relevant", "other",
}


@mcp.tool()
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
        "tip_id":          tip["id"],
        "tip_date":        tip["tip_date"],
        "tip_text":        tip["tip_text"],
        "feedback_status": tip["feedback_status"],
    })


@mcp.tool()
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


if __name__ == "__main__":
    mcp.run()
