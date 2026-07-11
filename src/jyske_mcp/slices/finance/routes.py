"""
The finance slice's HTTP routes, mounted by the platform (`web/app.py`) via
`api.router` (see .agent/epics/vsa-restructure-blueprint.md §4/§6).

Relocated out of jyske_mcp/web/app.py at epic deliverable #7b —
behavior-preserving move, no logic changes (including the inline
currency-de-blending in /budgets/breakdown, which is finance domain). The
platform's AuthMiddleware wraps every route regardless of which router it's
declared on, since it's added to the FastAPI app as middleware, not attached
per-router — so auth/session behavior is unchanged by this move.

Uses the finance `Storage` (in-slice import) for both finance-domain and
kernel-primitive reads (Storage extends KernelStorage — see
slices/finance/storage.py's docstring) and calls the kernel primitive
`recategorize_from_transaction` via that same Storage instance
(finance->kernel, allowed by the layering contract).
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jyske_mcp.kernel.categorizer import category_tree
from jyske_mcp.slices.finance.storage import Storage, PRIMARY_CURRENCY
from jyske_mcp.slices.finance.tools import goal_pace

router = APIRouter()


class TipFeedbackRequest(BaseModel):
    tip_id: int
    reason_text: str


class RecategorizeRequest(BaseModel):
    transaction_id: int  # maps to transactions.id (the primary key) — NOT
                         # transactions.transaction_id (the bank's own unique
                         # reference column).
    category_top: str
    category_mid: str


@router.get("/tip/today")
def tip_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tip = Storage().get_tip_for_date(today)
    if tip is None:
        return {"tip": None}
    return {"tip": tip.model_dump()}


@router.post("/tip/feedback")
def tip_feedback(req: TipFeedbackRequest):
    # UI path specifically — free-text only, no verdict choice (see
    # jyske_mcp/slices/finance/tips.py / tools.submit_tip_feedback for the chat
    # path, which always records an explicit accepted/rejected verdict instead).
    try:
        Storage().set_tip_feedback(
            req.tip_id, "evaluated", None, req.reason_text, source="ui"
        )
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True}


@router.get("/budgets/status")
def budgets_status():
    # Storage().get_budget_status() already returns list[dict] directly —
    # unlike tools.get_budget_status() (the MCP tool wrapper, dispatched via
    # the finance registry above), which json.dumps()s the same rows into a
    # string for the chat tool-call path. Call Storage directly here so the
    # dashboard gets real JSON, not a double-encoded string.
    return {"budgets": Storage().get_budget_status()}


@router.get("/budgets/breakdown")
def budgets_breakdown(category: str):
    # Sub-category drill-down for an expandable budget card: same month
    # window as /budgets/status (via current_month_window()) and the same
    # sum_spending() aggregation path, just grouped by mid instead of top —
    # so `total` here always matches that category's `spent` on the status
    # endpoint.
    #
    # Mirrors get_budget_status()'s DKK-primary treatment: `spent`/`total`
    # are PRIMARY_CURRENCY only (no exchange rate), non-DKK amounts are
    # surfaced separately via other_currency_amounts (per row and overall)
    # rather than blended in. `count` is just a line-item count, not a
    # monetary amount, so it's safe to total across currencies as before.
    storage = Storage()
    date_from, date_to = storage.current_month_window()
    rows = storage.sum_spending(date_from, date_to, category_top=category, group_by="mid")

    by_mid: dict[str | None, dict] = {}
    for row in rows:
        key = row["key"] or None
        entry = by_mid.setdefault(key, {"by_ccy": {}, "count": 0})
        entry["by_ccy"][row["currency"]] = round(
            entry["by_ccy"].get(row["currency"], 0.0) + row["amount"], 2
        )
        entry["count"] += row["count"]

    breakdown = []
    uncategorized_entry = None
    for key, entry in by_mid.items():
        by_ccy = entry["by_ccy"]
        spent = round(by_ccy.get(PRIMARY_CURRENCY, 0.0), 2)
        others = {c: a for c, a in by_ccy.items() if c != PRIMARY_CURRENCY and a}
        item = {
            "category_mid": key,
            "label":        key or "Uncategorized",
            "spent":        spent,
            "count":        entry["count"],
            "uncategorized": key is None,
        }
        if others:
            item["other_currency_amounts"] = others
        if key is None:
            uncategorized_entry = item
        else:
            breakdown.append(item)

    breakdown.sort(key=lambda x: x["spent"], reverse=True)
    if uncategorized_entry is not None:
        breakdown.append(uncategorized_entry)

    total = round(sum(item["spent"] for item in breakdown), 2)
    overall_others: dict[str, float] = {}
    for item in breakdown:
        for c, a in item.get("other_currency_amounts", {}).items():
            overall_others[c] = round(overall_others.get(c, 0.0) + a, 2)

    response = {
        "category":    category,
        "period_from": date_from,
        "period_to":   date_to,
        "total":       total,
        "breakdown":   breakdown,
    }
    if overall_others:
        response["other_currency_amounts"] = overall_others
    return response


@router.get("/budgets/transactions")
def budgets_transactions(
    category: str,
    mid: str | None = None,
    uncategorized: bool = False,
):
    # Line items backing a single row from /budgets/breakdown. Uses the same
    # month window (current_month_window()) and Storage().get_transactions_by_category(),
    # which mirrors sum_spending()'s filters exactly so these always sum to
    # that row's `spent` figure. Never returns raw_data.
    storage = Storage()
    date_from, date_to = storage.current_month_window()
    items = storage.get_transactions_by_category(
        date_from,
        date_to,
        category_top=category,
        category_mid=None if uncategorized else mid,
        uncategorized=uncategorized,
    )
    return {
        "category":     category,
        "category_mid": None if uncategorized else mid,
        "period_from":  date_from,
        "period_to":    date_to,
        "items":        [i.model_dump() for i in items],
    }


@router.get("/goals")
def goals():
    # Same reasoning as /budgets/status used to be: Storage().get_goals()
    # now returns list[GoalDTO] — model_dump() each one back to a plain
    # dict before merging in "pace". goal_pace() is the MCP tool (imported
    # above, reused as-is per convention) and returns a JSON string keyed by
    # "goal_id" per goal — matches GoalDTO's "id" field.
    base = Storage().get_goals()
    pace = {p["goal_id"]: p for p in json.loads(goal_pace())}
    return {"goals": [{**g.model_dump(), "pace": pace.get(g.id)} for g in base]}


@router.get("/budgets/categories")
def budgets_categories():
    # Single source of truth for the category picker (frontend) and for
    # validating /budgets/recategorize requests — both read category_tree().
    return {"tree": category_tree()}


@router.post("/budgets/recategorize")
def budgets_recategorize(req: RecategorizeRequest):
    tree = category_tree()
    if req.category_top not in tree:
        return JSONResponse(
            {"detail": f"Unknown category_top: {req.category_top}"}, status_code=400
        )
    if req.category_mid not in tree[req.category_top]:
        return JSONResponse(
            {"detail": f"Unknown category_mid: {req.category_mid} for {req.category_top}"},
            status_code=400,
        )

    result = Storage().recategorize_from_transaction(
        req.transaction_id, req.category_top, req.category_mid
    )
    if result is None:
        return JSONResponse({"detail": "Transaction not found"}, status_code=404)

    return {
        "ok": True,
        "raw_name": result["raw_name"],
        "old_category_top": result["old_category_top"],
        "new_category_top": req.category_top,
        "new_category_mid": req.category_mid,
        "transactions_updated": result["transactions_updated"],
    }
