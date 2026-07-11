"""
The finance slice's chat tool-call registry: the Anthropic-shaped schema
list (TOOLS), its LiteLLM/OpenAI-function-calling translation
(LITELLM_TOOLS), and the name->callable dispatcher (run_tool) the chat loop
uses to actually execute a tool call.

Relocated out of jyske_mcp/web/app.py at epic deliverable #7a
(.agent/epics/vsa-restructure-blueprint.md §4/§6) — behavior-preserving move,
no logic changes. Deliberately still three separate hand-maintained
structures (TOOLS, LITELLM_TOOLS, run_tool's dispatch table) rather than one
derived registry — collapsing them into a single ToolSpec source is epic
deliverable #8, not this move.
"""

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

TOOLS = [
    {
        "name": "get_memory",
        "description": (
            "Always call this at the start of every session.\n"
            "Returns the user profile (goals, preferences, known patterns)\n"
            "and the last 3 session summaries in a compact format."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_accounts",
        "description": "List all bank accounts from the active consent session.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_balances",
        "description": (
            "Get balances for one or all accounts.\n"
            "Leave account_uid empty to fetch all accounts.\n"
            "Use list_accounts to find account UIDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to fetch balances for. Leave empty for all accounts.",
                }
            },
        },
    },
    {
        "name": "get_transactions",
        "description": (
            "Get transactions for an account.\n"
            "Use list_accounts to find account UIDs.\n"
            "date_from and date_to are optional ISO dates (YYYY-MM-DD); defaults to last 30 days."
        ),
        "input_schema": {
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
    },
    {
        "name": "categorize_transaction",
        "description": (
            "Categorize a merchant by name and optional MCC code.\n\n"
            "Two-step flow:\n"
            "  - Call without llm_category: tries merchant cache then MCC lookup.\n"
            '    Returns the category on hit, or {"needs_llm": true, "raw_name": ...}\n'
            "    to signal that Claude should determine the category and call again.\n"
            '  - Call with llm_category (format "Top > Mid > Leaf"): stores the\n'
            "    LLM-derived category and returns it."
        ),
        "input_schema": {
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
    },
    {
        "name": "get_sync_status",
        "description": "Returns when data was last synced. Call this as part of every opening brief.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_budget",
        "description": (
            "Set a spending budget for a category.\n"
            "category must be a top-level category name from the taxonomy.\n"
            "period defaults to 'monthly'. Replaces any existing budget for that category+period."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Top-level category name (e.g. 'Food & Dining').",
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
    },
    {
        "name": "get_budget_status",
        "description": "Get current budget status. Always call this as part of the opening brief.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_memory",
        "description": (
            "Always call this at the end of every session.\n"
            "session_summary: 2-3 sentence plain language summary of what happened this session.\n"
            "profile_updates: JSON string of profile keys to update. Valid keys:\n"
            "  - 'preferences': how user likes data presented, language preference, categories they care about\n"
            "  - 'patterns': recurring behaviors or anomalies worth remembering long-term\n"
            "  - 'pending': things flagged but not resolved, awaiting follow-up next session\n"
            "Goals are no longer stored here — use set_goal / update_goal_progress.\n"
            "Only include keys that actually changed this session."
        ),
        "input_schema": {
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
    },
    {
        "name": "get_goals",
        "description": "Get all active goals with progress.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_goal",
        "description": "Create a new savings or spending goal.",
        "input_schema": {
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
    },
    {
        "name": "update_goal_progress",
        "description": "Update progress on a goal.",
        "input_schema": {
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
    },
    {
        "name": "get_onboarding_status",
        "description": "Check if budget onboarding is complete. Returns current stage if not.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_onboarding_stage",
        "description": (
            "Record progress through budget onboarding. Call once per stage as the user answers.\n"
            "Only pass the fields relevant to the stage just completed; stage moves the\n"
            "onboarding record to the next step ('income' -> 'fixed_costs' -> 'savings' -> 'style' -> 'complete')."
        ),
        "input_schema": {
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
    },
    {
        "name": "complete_onboarding",
        "description": "Mark budget onboarding as complete.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_overspend_patterns",
        "description": "Returns categories overspent 3+ consecutive months. Call monthly.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_spending",
        "description": (
            "Sum spending (debits only) between two ISO dates.\n"
            "Defaults date_from/date_to to the current calendar month if left empty.\n"
            "Use this instead of summing a get_transactions listing by hand."
        ),
        "input_schema": {
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
    },
    {
        "name": "compare_spending",
        "description": (
            "Compare total spending in one month against a baseline month (both 'YYYY-MM').\n"
            "Defaults month to the current calendar month and baseline_month to the month before it.\n"
            "Use this instead of eyeballing two get_transactions listings."
        ),
        "input_schema": {
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
    },
    {
        "name": "goal_pace",
        "description": (
            "Compute pacing math for active goals: percent complete, whether on track\n"
            "for the deadline, and the daily/monthly amount required to still hit it.\n"
            "goal_id = 0 (default) means all active goals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "integer",
                    "description": "ID of a specific goal, or 0 for all active goals.",
                },
            },
        },
    },
    {
        "name": "recurring_charges",
        "description": (
            "Detect recurring/subscription-like charges and frequent merchants from\n"
            "transaction history. Flags merchants that have gone quiet (needs_confirmation)\n"
            "so the agent can ask the user whether they cancelled it."
        ),
        "input_schema": {
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
    },
    {
        "name": "confirm_recurring_status",
        "description": (
            "Record the user's answer to a cancellation-confirmation question raised by\n"
            "recurring_charges (needs_confirmation: true). status must be 'active', 'cancelled', or 'unknown'."
        ),
        "input_schema": {
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
    },
    {
        "name": "get_current_tip",
        "description": (
            "Returns today's financial tip of the day, if one was generated overnight.\n"
            "Call this opportunistically — as part of the opening brief, or whenever\n"
            "the user's message could plausibly be reacting to a tip."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_tip_feedback",
        "description": (
            "Record the user's conversational reaction to a tip. verdict must be\n"
            "'accepted' or 'rejected' — always call this with an explicit verdict when\n"
            "the user pushes back on or endorses a tip. reason_text is required: capture\n"
            "the user's actual words/reasoning, never just the verdict."
        ),
        "input_schema": {
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
    },
]


# LiteLLM (and every non-Anthropic provider it talks to) expects tools in
# OpenAI's function-calling shape, not Anthropic's {name, description,
# input_schema}. Convert once at import time; litellm re-translates this
# into the Anthropic tool format under the hood when LLM_MODEL is a Claude
# model, so a single TOOLS list keeps working across providers.
def _tools_for_litellm(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


LITELLM_TOOLS = _tools_for_litellm(TOOLS)


def run_tool(name: str, inputs: dict) -> str:
    dispatch = {
        "get_memory":             lambda i: get_memory(),
        "list_accounts":          lambda i: list_accounts(),
        "get_balances":           lambda i: get_balances(**i),
        "get_transactions":       lambda i: get_transactions(**i),
        "categorize_transaction": lambda i: categorize_transaction(**i),
        "get_sync_status":        lambda i: get_sync_status(),
        "set_budget":             lambda i: set_budget(**i),
        "get_budget_status":      lambda i: get_budget_status(),
        "update_memory":          lambda i: update_memory(**i),
        "get_goals":              lambda i: get_goals(),
        "set_goal":               lambda i: set_goal(**i),
        "update_goal_progress":   lambda i: update_goal_progress(**i),
        "get_onboarding_status":  lambda i: get_onboarding_status(),
        "set_onboarding_stage":   lambda i: set_onboarding_stage(**i),
        "complete_onboarding":    lambda i: complete_onboarding(),
        "get_overspend_patterns": lambda i: get_overspend_patterns(),
        "get_spending":           lambda i: get_spending(**i),
        "compare_spending":       lambda i: compare_spending(**i),
        "goal_pace":              lambda i: goal_pace(**i),
        "recurring_charges":      lambda i: recurring_charges(**i),
        "confirm_recurring_status": lambda i: confirm_recurring_status(**i),
        "get_current_tip":        lambda i: get_current_tip(),
        "submit_tip_feedback":    lambda i: submit_tip_feedback(**i),
    }
    fn = dispatch.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return fn(inputs)
    except Exception as e:
        return f"Tool error ({name}): {e}"
