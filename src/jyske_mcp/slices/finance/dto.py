"""
Pydantic DTOs at the finance-domain storage seam (VSA restructure epic,
deliverable #5 — see .agent/epics/vsa-restructure-blueprint.md §2/§3).

These model the shapes FinanceStorage (jyske_mcp/slices/finance/storage.py) exchanges with
its callers: spending aggregation, budgets/budget-status, goals, recurring-
charge detection, onboarding, budget history/overspend patterns, and tips.

CRITICAL behavior-preservation note (blueprint §3): the golden-master tests
pin exact json.loads() key-sets INCLUDING conditionally-present keys —
BudgetStatusDTO.other_currency_amounts must be present only when non-empty,
never emitted as a `null`. A plain `model_dump()` would emit it as null and
break tests/test_mixed_currency_no_blend.py /
tests/test_budget_and_goal_endpoints.py. BudgetStatusDTO.to_dict() below is
the custom serializer that reproduces the conditional omission.

Several of these Storage methods are ALSO exercised directly (not through a
JSON-serializing tool/endpoint) by pinned tests using dict subscript / `in` /
`isinstance(x, dict)` on the raw return value (e.g.
tests/test_sum_spending.py's `rows[0]["amount"]`,
tests/test_mixed_currency_no_blend.py's `assert_no_blend` recursive
isinstance(dict) walk, tests/jobs/test_sync_dedup_and_categorization.py's
MagicMock `storage.get_budget_status.return_value = []`). For exactly those
methods (sum_spending, get_budget_status — and get_last_sync in
kernel/dto.py's SyncRecordDTO, forced by jyske_mcp.kernel.sync.is_sync_stale's
own pinned dict-shaped unit tests) Storage keeps returning plain
dict/list[dict], constructing the DTO internally for validation/typing and
converting back via model_dump()/to_dict() before returning — the "keep
conditional-key assembly reachable, DTOs for the stable shape" strategy the
blueprint sanctions as an alternative to a literal DTO return type. Every
other method below returns the DTO itself; callers were updated to consume
it (see storage.py's FinanceStorage and its callers in mcp/server.py,
web/app.py, jobs/tips.py).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SpendingRowDTO(BaseModel):
    """One grouped-aggregation row from FinanceStorage.sum_spending —
    always folded per currency, never blended (see PRIMARY_CURRENCY in
    storage.py)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str | None
    currency: str
    amount: float
    count: int


class BudgetDTO(BaseModel):
    """One row from the `budgets` table (FinanceStorage.get_budgets)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    category_top: str
    category_mid: str | None
    limit_amount: float
    period: str
    created_at: float


class BudgetStatusDTO(BaseModel):
    """One category's current-period budget status
    (FinanceStorage.get_budget_status). spent/percent/status are
    PRIMARY_CURRENCY (DKK) only; other_currency_amounts surfaces non-DKK
    spend separately and MUST be omitted entirely (not `null`) when there
    is none — see module docstring and to_dict() below.

    id is the underlying budgets.id row id — the frontend needs it to call
    DELETE /budgets/{id} on a specific row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    category: str
    category_mid: str | None
    spent: float
    limit: float
    period: str
    percent: float
    status: str
    other_currency_amounts: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        """model_dump() with other_currency_amounts OMITTED (not `null`)
        when unset — the exact conditional-key shape the golden-master
        tests pin. Every other field is always present, including
        category_mid when it's None (a real, meaningful "no mid-category"
        value, unlike other_currency_amounts's all-or-nothing presence)."""
        d = self.model_dump(exclude={"other_currency_amounts"})
        if self.other_currency_amounts:
            d["other_currency_amounts"] = self.other_currency_amounts
        return d


class TransactionLineDTO(BaseModel):
    """One compact transaction line backing a budget breakdown row
    (FinanceStorage.get_transactions_by_category) — never raw_data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    date: str
    amount: float | None
    currency: str | None
    description: str | None


class GoalDTO(BaseModel):
    """One row from the `goals` table (FinanceStorage.get_goals)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    target_amount: float | None
    current_amount: float | None
    purpose: str | None
    deadline: str | None
    created_at: float
    updated_at: float


class RecurringCandidateDTO(BaseModel):
    """A candidate merchant/currency pair for recurring-charge
    classification (FinanceStorage.get_recurring_candidates), carrying the
    raw chronological (date, amount) charge sequence the classification
    logic in mcp/server.py needs to detect price-change runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merchant: str
    currency: str
    charges: list[tuple[str, float]] = Field(default_factory=list)
    categories: list[str | None] = Field(default_factory=list)


class RecurringStatusDTO(BaseModel):
    """A user-confirmed active/cancelled status for one (merchant, currency)
    pair (FinanceStorage.get_recurring_statuses)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    confirmed_at: float


class OnboardingDTO(BaseModel):
    """The single onboarding row for one agent (FinanceStorage.get_onboarding).
    fixed_costs is whatever the stored JSON parses to (list/dict/scalar), or
    the raw string on a parse failure — same dynamic shape
    get_onboarding()/set_onboarding_stage() have always carried, hence Any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    income: float | None
    income_day: int | None
    fixed_costs: Any = None
    savings_monthly: float | None
    savings_purpose: str | None
    savings_target: float | None
    savings_deadline: str | None
    budget_style: str | None
    completed_at: float | None
    updated_at: float


class BudgetHistoryEntryDTO(BaseModel):
    """One monthly snapshot from `budget_history`
    (FinanceStorage.get_budget_history)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    month: str
    period: str
    limit_amount: float
    actual_amount: float
    variance: float
    created_at: float


class OverspendPatternDTO(BaseModel):
    """A category overspent for `consecutive_months` running
    (FinanceStorage.get_overspend_patterns)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category_top: str
    consecutive_months: int
    months: list[str]
    avg_variance: float


class TipDTO(BaseModel):
    """One row from the `tips` table (FinanceStorage.get_tip_for_date/
    get_recent_tips_with_feedback/get_all_tips_with_feedback)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    agent_id: str
    created_at: float
    tip_date: str
    window_from: str
    window_to: str
    tip_text: str
    subject_key: str | None
    category_top: str | None
    based_on: str | None
    signals_json: str
    model: str
    prompt_version: str
    feedback_status: str
    feedback_reason_code: str | None
    feedback_reason_text: str | None
    feedback_source: str | None
    feedback_at: float | None
