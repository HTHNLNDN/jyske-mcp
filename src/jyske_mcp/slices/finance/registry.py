"""
The finance slice's chat tool-call registry: a single ToolSpec/ToolRegistry
source deriving the Anthropic-shaped schema list, its LiteLLM/OpenAI
function-calling translation, and the name->callable dispatcher the chat
loop uses to actually execute a tool call.

Relocated out of jyske_mcp/web/app.py at epic deliverable #7a
(.agent/epics/vsa-restructure-blueprint.md §4/§6) as three separate
hand-maintained structures (TOOLS, LITELLM_TOOLS, run_tool's dispatch
table); collapsed into this single TOOL_REGISTRY at epic deliverable #8
(§3/§6) — the LiteLLM schema and the dispatch table are now both generated
from the same ToolSpec list instead of hand-maintained in parallel.
"""

import inspect
from dataclasses import dataclass
from typing import Callable

from jyske_mcp.slices.finance.tools import (
    list_accounts,
    get_balances,
    get_transactions,
    categorize_transaction,
    get_sync_status,
    get_memory,
    update_memory,
    set_budget,
    get_budget_status,
    get_goals,
    set_goal,
    update_goal_progress,
    get_onboarding_status,
    set_onboarding_stage,
    complete_onboarding,
    get_overspend_patterns,
    get_spending,
    compare_spending,
    goal_pace,
    recurring_charges,
    confirm_recurring_status,
    get_current_tip,
    submit_tip_feedback,
)


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool's Anthropic-shaped schema paired with the Python
    callable that implements it — the single hand-maintained unit that
    TOOLS/LITELLM_TOOLS/run_tool's dispatch dict collapse into (epic
    deliverable #8, .agent/epics/vsa-restructure-blueprint.md §3/§6)."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[..., str]


class ToolRegistry:
    """An ordered list of ToolSpec — the single source the Anthropic schema,
    the LiteLLM/OpenAI schema, and the dispatch table all derive from."""

    def __init__(self, specs: list[ToolSpec]):
        self._specs = list(specs)
        self._by_name = {s.name: s for s in self._specs}

    def anthropic_schemas(self) -> list[dict]:
        """Reproduces the old TOOLS shape exactly: {name, description,
        input_schema} per spec, same order."""
        return [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s in self._specs
        ]

    def litellm_schemas(self) -> list[dict]:
        """Reproduces the old LITELLM_TOOLS shape exactly. LiteLLM (and
        every non-Anthropic provider it talks to) expects tools in OpenAI's
        function-calling shape, not Anthropic's {name, description,
        input_schema}; litellm re-translates this back into the Anthropic
        tool format under the hood when LLM_MODEL is a Claude model, so this
        one derived list keeps working across providers."""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.input_schema,
                },
            }
            for s in self._specs
        ]

    def dispatch(self, name: str, inputs: dict) -> str:
        """Reproduces the old run_tool exactly: unknown name -> "Unknown
        tool: {name}"; handler raising -> "Tool error ({name}): {e}"; the
        zero-arg-vs-kwargs split is driven off the handler's own signature
        being empty or not (today's 9 zero-arg tools have no params and
        ignore whatever `inputs` was passed; the rest take **inputs).

        Deliberately kept at two args (name, inputs) — threading agent_id
        through is epic deliverable #9, not this one; today's hardcoded
        "finance" scoping inside each tool is preserved unchanged."""
        spec = self._by_name.get(name)
        if spec is None:
            return f"Unknown tool: {name}"
        try:
            if inspect.signature(spec.handler).parameters:
                return spec.handler(**inputs)
            return spec.handler()
        except Exception as e:
            return f"Tool error ({name}): {e}"


TOOL_REGISTRY = ToolRegistry([
    ToolSpec(
        name="get_memory",
        description=(
            "Always call this at the start of every session.\n"
            "Returns the user profile (goals, preferences, known patterns)\n"
            "and the last 3 session summaries in a compact format."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=get_memory,
    ),
    ToolSpec(
        name="list_accounts",
        description="List all bank accounts from the active consent session.",
        input_schema={"type": "object", "properties": {}},
        handler=list_accounts,
    ),
    ToolSpec(
        name="get_balances",
        description=(
            "Get balances for one or all accounts.\n"
            "Leave account_uid empty to fetch all accounts.\n"
            "Use list_accounts to find account UIDs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to fetch balances for. Leave empty for all accounts.",
                }
            },
        },
        handler=get_balances,
    ),
    ToolSpec(
        name="get_transactions",
        description=(
            "Get transactions for an account.\n"
            "Use list_accounts to find account UIDs.\n"
            "date_from and date_to are optional ISO dates (YYYY-MM-DD); defaults to last 30 days."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to fetch transactions for.",
                },
                "date_from": {
                    "type": "string",
                    "description": "Start date in ISO format YYYY-MM-DD.",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date in ISO format YYYY-MM-DD.",
                },
            },
            "required": ["account_uid"],
        },
        handler=get_transactions,
    ),
    ToolSpec(
        name="categorize_transaction",
        description=(
            "Categorize a merchant by name and optional MCC code.\n\n"
            "Two-step flow:\n"
            "  - Call without llm_category: tries the merchant cache.\n"
            '    Returns the category on hit, or {"needs_llm": true, "raw_name": ...}\n'
            "    to signal that Claude should determine the category and call again.\n"
            '  - Call with llm_category (format "Top > Mid > Leaf"): stores the\n'
            "    LLM-derived category and returns it. Mid must be one of that top\n"
            "    category's existing sub-categories (or blank) — an unknown mid is\n"
            "    rejected with the valid list, so pick from it, don't invent one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "raw_name": {
                    "type": "string",
                    "description": "Raw merchant name from transaction.",
                },
                "mcc": {
                    "type": "string",
                    "description": "Optional merchant category code.",
                },
                "llm_category": {
                    "type": "string",
                    "description": "Optional LLM-derived category in 'Top > Mid > Leaf' format.",
                },
            },
            "required": ["raw_name"],
        },
        handler=categorize_transaction,
    ),
    ToolSpec(
        name="get_sync_status",
        description="Returns when data was last synced. Call this as part of every opening brief.",
        input_schema={"type": "object", "properties": {}},
        handler=get_sync_status,
    ),
    ToolSpec(
        name="set_budget",
        description=(
            "Set a spending budget for a category.\n"
            "category must be a top-level category name from the taxonomy.\n"
            "period defaults to 'monthly'. Replaces any existing budget for that category+period."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Top-level category name (e.g. 'Bills').",
                },
                "limit_amount": {
                    "type": "number",
                    "description": "Spending limit for the period.",
                },
                "period": {
                    "type": "string",
                    "description": "Budget period — 'monthly' (default) or 'weekly'.",
                },
            },
            "required": ["category", "limit_amount"],
        },
        handler=set_budget,
    ),
    ToolSpec(
        name="get_budget_status",
        description="Get current budget status. Always call this as part of the opening brief.",
        input_schema={"type": "object", "properties": {}},
        handler=get_budget_status,
    ),
    ToolSpec(
        name="update_memory",
        description=(
            "Always call this at the end of every session.\n"
            "session_summary: 2-3 sentence plain language summary of what happened this session.\n"
            "profile_updates: JSON string of profile keys to update. Valid keys:\n"
            "  - 'preferences': how user likes data presented, language preference, categories they care about\n"
            "  - 'patterns': recurring behaviors or anomalies worth remembering long-term\n"
            "  - 'pending': things flagged but not resolved, awaiting follow-up next session\n"
            "Goals are no longer stored here — use set_goal / update_goal_progress.\n"
            "Only include keys that actually changed this session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_summary": {
                    "type": "string",
                    "description": "2-3 sentence summary of the session.",
                },
                "profile_updates": {
                    "type": "string",
                    "description": "JSON string of profile keys to update.",
                },
            },
            "required": ["session_summary"],
        },
        handler=update_memory,
    ),
    ToolSpec(
        name="get_goals",
        description="Get all active goals with progress.",
        input_schema={"type": "object", "properties": {}},
        handler=get_goals,
    ),
    ToolSpec(
        name="set_goal",
        description="Create a new savings or spending goal.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short name for the goal.",
                },
                "target_amount": {
                    "type": "number",
                    "description": "Amount to reach.",
                },
                "purpose": {
                    "type": "string",
                    "description": "What the goal is for.",
                },
                "deadline": {
                    "type": "string",
                    "description": "Target date for the goal (ISO date or free text).",
                },
            },
            "required": ["name", "target_amount", "purpose", "deadline"],
        },
        handler=set_goal,
    ),
    ToolSpec(
        name="update_goal_progress",
        description="Update progress on a goal.",
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "integer",
                    "description": "ID of the goal to update.",
                },
                "current_amount": {
                    "type": "number",
                    "description": "New current progress amount.",
                },
            },
            "required": ["goal_id", "current_amount"],
        },
        handler=update_goal_progress,
    ),
    ToolSpec(
        name="get_onboarding_status",
        description="Check if budget onboarding is complete. Returns current stage if not.",
        input_schema={"type": "object", "properties": {}},
        handler=get_onboarding_status,
    ),
    ToolSpec(
        name="set_onboarding_stage",
        description=(
            "Record progress through budget onboarding. Call once per stage as the user answers.\n"
            "Only pass the fields relevant to the stage just completed; stage moves the\n"
            "onboarding record to the next step ('income' -> 'fixed_costs' -> 'savings' -> 'style' -> 'complete')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Next onboarding stage: 'fixed_costs', 'savings', 'style', or 'complete'.",
                },
                "income": {
                    "type": "number",
                    "description": "Take-home income amount.",
                },
                "income_day": {
                    "type": "integer",
                    "description": "Day of month income usually lands.",
                },
                "fixed_costs": {
                    "type": "string",
                    "description": "Recurring non-negotiable costs, as free text or JSON.",
                },
                "savings_monthly": {
                    "type": "number",
                    "description": "Amount to save per month toward the goal.",
                },
                "savings_purpose": {
                    "type": "string",
                    "description": "What the savings are for.",
                },
                "savings_target": {
                    "type": "number",
                    "description": "Total savings target amount.",
                },
                "savings_deadline": {
                    "type": "string",
                    "description": "Target date for the savings goal.",
                },
                "budget_style": {
                    "type": "string",
                    "description": "How blunt the user wants budget talk to be, e.g. 'honest' or 'gentle'.",
                },
            },
            "required": ["stage"],
        },
        handler=set_onboarding_stage,
    ),
    ToolSpec(
        name="complete_onboarding",
        description="Mark budget onboarding as complete.",
        input_schema={"type": "object", "properties": {}},
        handler=complete_onboarding,
    ),
    ToolSpec(
        name="get_overspend_patterns",
        description="Returns categories overspent 3+ consecutive months. Call monthly.",
        input_schema={"type": "object", "properties": {}},
        handler=get_overspend_patterns,
    ),
    ToolSpec(
        name="get_spending",
        description=(
            "Sum spending (debits only) between two ISO dates.\n"
            "Defaults date_from/date_to to the current calendar month if left empty.\n"
            "Use this instead of summing a get_transactions listing by hand."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "Start date in ISO format YYYY-MM-DD. Defaults to the 1st of the current month.",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date in ISO format YYYY-MM-DD. Defaults to today.",
                },
                "category": {
                    "type": "string",
                    "description": "Top-level category name to narrow the sum to. Leave empty for all categories.",
                },
                "group_by": {
                    "type": "string",
                    "description": "'category' (default), 'mid', 'month', or 'none'.",
                },
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to narrow the sum to. Leave empty for all accounts.",
                },
            },
        },
        handler=get_spending,
    ),
    ToolSpec(
        name="compare_spending",
        description=(
            "Compare total spending in one month against a baseline month (both 'YYYY-MM').\n"
            "Defaults month to the current calendar month and baseline_month to the month before it.\n"
            "Use this instead of eyeballing two get_transactions listings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "Month to compare, 'YYYY-MM'. Defaults to the current month.",
                },
                "baseline_month": {
                    "type": "string",
                    "description": "Baseline month, 'YYYY-MM'. Defaults to the month before `month`.",
                },
                "category": {
                    "type": "string",
                    "description": "Top-level category to narrow to (breaks down by mid-category instead).",
                },
            },
        },
        handler=compare_spending,
    ),
    ToolSpec(
        name="goal_pace",
        description=(
            "Compute pacing math for active goals: percent complete, whether on track\n"
            "for the deadline, and the daily/monthly amount required to still hit it.\n"
            "goal_id = 0 (default) means all active goals."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "integer",
                    "description": "ID of a specific goal, or 0 for all active goals.",
                },
            },
        },
        handler=goal_pace,
    ),
    ToolSpec(
        name="recurring_charges",
        description=(
            "Detect recurring/subscription-like charges and frequent merchants from\n"
            "transaction history. Flags merchants that have gone quiet (needs_confirmation)\n"
            "so the agent can ask the user whether they cancelled it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "How many days of history to scan. Defaults to 180.",
                },
                "min_count": {
                    "type": "integer",
                    "description": "Minimum charge count to consider a merchant. Defaults to 3.",
                },
            },
        },
        handler=recurring_charges,
    ),
    ToolSpec(
        name="confirm_recurring_status",
        description=(
            "Record the user's answer to a cancellation-confirmation question raised by\n"
            "recurring_charges (needs_confirmation: true). status must be 'active', 'cancelled', or 'unknown'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "merchant": {
                    "type": "string",
                    "description": "Merchant name as shown by recurring_charges.",
                },
                "status": {
                    "type": "string",
                    "description": "'active', 'cancelled', or 'unknown'.",
                },
                "currency": {
                    "type": "string",
                    "description": "Currency of the recurring charge. Defaults to 'DKK'.",
                },
            },
            "required": ["merchant", "status"],
        },
        handler=confirm_recurring_status,
    ),
    ToolSpec(
        name="get_current_tip",
        description=(
            "Returns today's financial tip of the day, if one was generated overnight.\n"
            "Call this opportunistically — as part of the opening brief, or whenever\n"
            "the user's message could plausibly be reacting to a tip."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=get_current_tip,
    ),
    ToolSpec(
        name="submit_tip_feedback",
        description=(
            "Record the user's conversational reaction to a tip. verdict must be\n"
            "'accepted' or 'rejected' — always call this with an explicit verdict when\n"
            "the user pushes back on or endorses a tip. reason_text is required: capture\n"
            "the user's actual words/reasoning, never just the verdict."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tip_id": {
                    "type": "integer",
                    "description": "ID of the tip, from get_current_tip.",
                },
                "verdict": {
                    "type": "string",
                    "description": "'accepted' or 'rejected'.",
                },
                "reason_text": {
                    "type": "string",
                    "description": "The user's actual reasoning — required.",
                },
                "reason_code": {
                    "type": "string",
                    "description": (
                        "Optional classifier: not_representative, already_addressed, "
                        "not_actionable, inaccurate, not_relevant, other."
                    ),
                },
            },
            "required": ["tip_id", "verdict", "reason_text"],
        },
        handler=submit_tip_feedback,
    ),
])
