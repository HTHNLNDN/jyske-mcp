"""
The finance slice's public interface — the only surface `platform/` code
is allowed to import from `jyske_mcp.slices.finance` (see
.agent/epics/vsa-restructure-blueprint.md §4).

Exposes: the tool registry (TOOLS/LITELLM_TOOLS/run_tool — still three
separate hand-maintained structures, per the epic's #7a/#8 split; see
registry.py's docstring), the system PROMPT, the nightly run_tips/run_evals
jobs, the post-sync snapshot_budget_history hook (from #6), the finance
HTTP routes (`router`, moved into `routes.py` at #7b — mounted by
`web/app.py` via `app.include_router(api.router)`), and `audit_section`
(the finance portion of `/audit/data`, also from #7b).

`goal_pace` is no longer re-exported here — the only caller outside the
chat tool-dispatch path was app.py's /goals route, and that route moved
into `routes.py` at #7b, where it imports `goal_pace` as an in-slice
sibling import instead. See routes.py's `goals()`.
"""

from pathlib import Path

from jyske_mcp.slices.finance.storage import Storage
from jyske_mcp.slices.finance.registry import TOOLS, LITELLM_TOOLS, run_tool
from jyske_mcp.slices.finance.routes import router
from jyske_mcp.slices.finance.tips import run_tips
from jyske_mcp.slices.finance.evals import run_evals

AGENT_ID = "finance"

PROMPT = (Path(__file__).parent / "prompt.md").read_text()


def snapshot_budget_history(agent_id: str = AGENT_ID) -> None:
    """Snapshot each active budget's spend-to-date so pattern detection
    (get_overspend_patterns) always has fresh data to work from.

    Lifted out of jyske_mcp.kernel.sync.run_sync's tail (the kernel->finance
    back-edge the blueprint's "Two cross-layer couplings" section flags) —
    kernel must never call finance-domain storage. The platform scheduler's
    daily_sync job (jyske_mcp/jobs/scheduler.py) calls run_sync() then this
    function, in that order, inside the SAME job — never as a second sync
    execution path (jyske_mcp/jobs/scheduler.py remains sync's single
    owner).
    """
    storage = Storage()
    for row in storage.get_budget_status(agent_id=agent_id):
        storage.record_budget_history(
            agent_id=agent_id,
            category_top=row["category"],
            period=row["period"],
            limit_amount=row["limit"],
            actual_amount=row["spent"],
        )


def audit_section(agent_id: str = AGENT_ID) -> dict:
    """The finance portion of `/audit/data` — budgets/goals/tips, exactly as
    web/app.py assembled them inline before #7b. The platform's /audit/data
    route stays in app.py and merges this dict's keys with the kernel keys
    (profile/summaries/transactions) it reads from KernelStorage directly."""
    storage = Storage()
    return {
        "budgets": storage.get_budget_status(agent_id),
        "goals": [g.model_dump() for g in storage.get_goals(agent_id)],
        "tips": [t.model_dump() for t in storage.get_all_tips_with_feedback(agent_id)],
    }
