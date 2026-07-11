"""
The finance slice's public interface — the only surface `platform/` code
is allowed to import from `jyske_mcp.slices.finance` (see
.agent/epics/vsa-restructure-blueprint.md §4). This module is deliberately
partial at deliverable #6: it exports only the post-sync budget-history
snapshot hook lifted out of jyske_mcp.kernel.sync.run_sync. The rest of the
blueprint's §4 surface (TOOL_REGISTRY, PROMPT, router, run_tips, run_evals,
audit_section) lands at deliverables #7/#8 when the tool/route/prompt code
itself moves into this slice.
"""

from jyske_mcp.slices.finance.storage import Storage

AGENT_ID = "finance"


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
