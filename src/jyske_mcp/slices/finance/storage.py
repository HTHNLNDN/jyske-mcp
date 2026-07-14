import json
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from jyske_mcp.kernel.storage import KernelStorage
from jyske_mcp.slices.finance.dto import (
    BudgetDTO,
    BudgetHistoryEntryDTO,
    BudgetStatusDTO,
    GoalDTO,
    OnboardingDTO,
    OverspendPatternDTO,
    RecurringCandidateDTO,
    RecurringStatusDTO,
    SpendingRowDTO,
    TipDTO,
    TransactionLineDTO,
)

# The only currency get_budget_status()'s spent/percent/status figures are
# computed against — budgets are set in DKK and there's no exchange rate, so
# non-DKK spend is surfaced separately (other_currency_amounts) rather than
# blended in. See the currency de-blending fix this constant is part of.
PRIMARY_CURRENCY = "DKK"


class FinanceStorage(KernelStorage):
    """Finance-domain storage surface: spending aggregation (the
    `direction != 'CRDT'` money math), budgets/budget-status, goals,
    recurring-charge detection, onboarding, budget history/overspend
    patterns, tips. See
    .agent/epics/vsa-restructure-blueprint.md §2 for the exact bucketing.

    Extends KernelStorage (rather than composing/opening its own
    sqlite3.connect) specifically to reuse its `_db()` connection primitive
    — the single kernel-owned connect() the blueprint requires both
    surfaces obtain their connection from, so tests/conftest.py's
    full_schema_storage fixture (which monkeypatches the module-level
    _CACHE_DB/CONFIG_DIR globals on jyske_mcp.kernel.storage, where _db()
    actually looks them up) redirects both surfaces identically. This also
    mirrors the real import direction the layering contract enforces:
    slices/finance may import kernel, never the reverse (see pyproject.toml's
    "Kernel imports nothing upward" import-linter contract).

    Physically relocated here from jyske_mcp/storage.py at deliverable #6.
    """

    # ── spending aggregation ─────────────────────────────────────────────────

    def sum_spending(
        self,
        date_from: str,
        date_to: str,
        category_top: str | None = None,
        account_uid: str | None = None,
        group_by: str = "category",
    ) -> list[dict[str, Any]]:
        """
        Sum debit spending (direction != 'CRDT') between two ISO dates,
        grouped by the requested key. Always grouped by currency too — today
        every row is DKK, but this stops a future non-DKK account from
        silently blending into a DKK total (no currency conversion here,
        just corruption avoidance).

        Returns plain dict rows (not SpendingRowDTO) — tests/test_sum_spending.py
        and tests/test_direction_null_undercount_bug.py subscript the raw
        return value directly (`rows[0]["amount"]`), so this stays
        dict-shaped; SpendingRowDTO is still built per row for validation/
        typing, then converted back via model_dump().
        """
        group_cols = {
            "category": "category_top",
            "mid":      "category_mid",
            "month":    "substr(date, 1, 7)",
            "none":     None,
        }
        if group_by not in group_cols:
            raise ValueError(
                f"Invalid group_by: {group_by!r}. Must be one of {sorted(group_cols)}"
            )
        group_col = group_cols[group_by]
        select_key = group_col if group_col is not None else "NULL"

        query = (
            f"SELECT {select_key}, currency, SUM(amount), COUNT(*) "
            "FROM transactions WHERE direction != 'CRDT' AND date BETWEEN ? AND ?"
        )
        params: list[Any] = [date_from, date_to]
        if category_top:
            query += " AND category_top = ?"
            params.append(category_top)
        if account_uid:
            query += " AND account_uid = ?"
            params.append(account_uid)
        query += f" GROUP BY {select_key}, currency"

        conn = self._db()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [
            SpendingRowDTO(
                key=row[0], currency=row[1], amount=round(row[2] or 0.0, 2), count=row[3]
            ).model_dump()
            for row in rows
        ]

    def get_transactions_by_category(
        self,
        date_from: str,
        date_to: str,
        category_top: str,
        category_mid: str | None = None,
        uncategorized: bool = False,
        account_uid: str | None = None,
    ) -> list[TransactionLineDTO]:
        """
        Compact transaction rows (never raw_data — see the no-raw-transaction-
        data rule) for a single category/mid, newest first. Filtering MUST
        mirror sum_spending()'s exactly (direction != 'CRDT', date BETWEEN,
        category_top =) so line items always reconcile with the aggregate
        totals shown above them in the UI.
        """
        query = (
            "SELECT id, date, amount, currency, description "
            "FROM transactions WHERE direction != 'CRDT' AND date BETWEEN ? AND ? "
            "AND category_top = ?"
        )
        params: list[Any] = [date_from, date_to, category_top]
        if uncategorized:
            query += " AND (category_mid IS NULL OR category_mid = '')"
        elif category_mid is not None:
            query += " AND category_mid = ?"
            params.append(category_mid)
        if account_uid:
            query += " AND account_uid = ?"
            params.append(account_uid)
        query += " ORDER BY date DESC"

        conn = self._db()
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [
            TransactionLineDTO(id=r[0], date=r[1], amount=r[2], currency=r[3], description=r[4])
            for r in rows
        ]

    # ── budgets ──────────────────────────────────────────────────────────────

    def set_budget(
        self,
        category_top: str,
        limit_amount: float,
        period: str,
        category_mid: str | None = None,
        agent_id: str = "finance",
    ) -> None:
        conn = self._db()
        if category_mid is None:
            conn.execute(
                "UPDATE budgets SET active = 0 "
                "WHERE category_top = ? AND category_mid IS NULL AND period = ? "
                "AND agent_id = ? AND active = 1",
                (category_top, period, agent_id),
            )
        else:
            conn.execute(
                "UPDATE budgets SET active = 0 "
                "WHERE category_top = ? AND category_mid = ? AND period = ? "
                "AND agent_id = ? AND active = 1",
                (category_top, category_mid, period, agent_id),
            )
        conn.execute(
            "INSERT INTO budgets (category_top, category_mid, limit_amount, period, active, created_at, agent_id) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (category_top, category_mid, limit_amount, period, time.time(), agent_id),
        )
        conn.commit()
        conn.close()

    def get_budgets(self, agent_id: str = "finance") -> list[BudgetDTO]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, category_top, category_mid, limit_amount, period, created_at "
            "FROM budgets WHERE active = 1 AND agent_id = ? ORDER BY category_top, category_mid",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [
            BudgetDTO(id=r[0], category_top=r[1], category_mid=r[2], limit_amount=r[3], period=r[4], created_at=r[5])
            for r in rows
        ]

    def deactivate_budget(self, budget_id: int, agent_id: str = "finance") -> bool:
        """Soft-deletes (active=0). Returns True if a row was actually
        deactivated (existed, was active, matched agent_id), False
        otherwise -- the caller needs this to return 404 vs 200. Unlike
        deactivate_goal (below), this filters by agent_id too, since it's
        reachable from an authenticated HTTP route (defense in depth
        against an id from a different agent scope, even though only one
        agent exists today)."""
        conn = self._db()
        cur = conn.execute(
            "UPDATE budgets SET active = 0 WHERE id = ? AND agent_id = ? AND active = 1",
            (budget_id, agent_id),
        )
        conn.commit()
        changed = cur.rowcount > 0
        conn.close()
        return changed

    def current_month_window(self) -> tuple[str, str]:
        """(month_start, today) as ISO dates -- the single source of truth for
        the 'this month' budget window, shared by get_budget_status and the
        breakdown/line-items endpoints so they can never drift."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        return month_start, today

    def get_budget_status(self, agent_id: str = "finance") -> list[dict[str, Any]]:
        """Returns plain dict rows (not BudgetStatusDTO) — pinned tests
        (tests/test_budget_status_mid_level.py, tests/test_mixed_currency_no_blend.py,
        tests/test_direction_null_undercount_bug.py) subscript/`in`-check the
        raw return value directly, and test_mixed_currency_no_blend.py's
        assert_no_blend() recursively isinstance(x, dict)-walks it — so this
        stays dict-shaped. BudgetStatusDTO.to_dict() (see
        slices/finance/dto.py) is still what builds each entry, reproducing
        other_currency_amounts's conditional presence exactly."""
        month_start, today = self.current_month_window()

        # category_top/direction are now reliable columns (see the migration
        # that added `direction` and backfilled categories), so this can go
        # through the same aggregation path as get_spending/compare_spending
        # instead of a divergent raw_data-parsing loop.
        #
        # Budgets are set (and scored) in PRIMARY_CURRENCY only — there's no
        # exchange rate, so non-DKK spend must never be blended into `spent`.
        # Keep the fold per-currency and pick out PRIMARY_CURRENCY below;
        # anything else is surfaced separately as other_currency_amounts.
        spending_rows = self.sum_spending(month_start, today, group_by="category")
        spending: dict[str, dict[str, float]] = {}   # cat -> {currency: amount}
        for row in spending_rows:
            cat = row["key"] or "Other"
            spending.setdefault(cat, {})
            spending[cat][row["currency"]] = round(
                spending[cat].get(row["currency"], 0.0) + row["amount"], 2
            )

        conn = self._db()
        budget_rows = conn.execute(
            "SELECT id, category_top, category_mid, limit_amount, period "
            "FROM budgets WHERE active = 1 AND agent_id = ?",
            (agent_id,),
        ).fetchall()
        conn.close()

        result = []
        for budget_id, cat_top, cat_mid, limit_amount, period in budget_rows:
            if cat_mid:
                # Mid-level budget: don't reuse the top-level aggregate above
                # (that would blend in every other sub-category under the
                # same top category). Query mid-level spend scoped to this
                # top category only, so a same-named mid under a different
                # top category can never leak in.
                mid_rows = self.sum_spending(
                    month_start, today, category_top=cat_top, group_by="mid"
                )
                by_ccy: dict[str, float] = {}
                for row in mid_rows:
                    if row["key"] == cat_mid:
                        by_ccy[row["currency"]] = round(
                            by_ccy.get(row["currency"], 0.0) + row["amount"], 2
                        )
            else:
                by_ccy = spending.get(cat_top, {})

            spent = round(by_ccy.get(PRIMARY_CURRENCY, 0.0), 2)
            others = {c: a for c, a in by_ccy.items() if c != PRIMARY_CURRENCY and a}

            percent = round((spent / limit_amount) * 100, 1) if limit_amount > 0 else 0.0
            if percent < 80:
                status = "on_track"
            elif percent <= 100:
                status = "warning"
            else:
                status = "over"

            dto = BudgetStatusDTO(
                id=budget_id,
                category=cat_top,
                category_mid=cat_mid,
                spent=spent,
                limit=limit_amount,
                period=period,
                percent=percent,
                status=status,
                other_currency_amounts=others or None,
            )
            result.append(dto.to_dict())
        return result

    # ── goals ────────────────────────────────────────────────────────────────

    def get_goals(self, agent_id: str = "finance") -> list[GoalDTO]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, name, target_amount, current_amount, purpose, deadline, created_at, updated_at "
            "FROM goals WHERE agent_id = ? AND active = 1 ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [
            GoalDTO(
                id=r[0], name=r[1], target_amount=r[2], current_amount=r[3],
                purpose=r[4], deadline=r[5], created_at=r[6], updated_at=r[7],
            )
            for r in rows
        ]

    def set_goal(
        self,
        agent_id: str,
        name: str,
        target_amount: float,
        purpose: str,
        deadline: str,
    ) -> int:
        now = time.time()
        conn = self._db()
        cur = conn.execute(
            "INSERT INTO goals "
            "(agent_id, name, target_amount, current_amount, purpose, deadline, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?, 1, ?, ?)",
            (agent_id, name, target_amount, purpose, deadline, now, now),
        )
        goal_id = cur.lastrowid
        conn.commit()
        conn.close()
        assert goal_id is not None  # always set after an INSERT
        return goal_id

    def update_goal_progress(self, goal_id: int, current_amount: float) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE goals SET current_amount = ?, updated_at = ? WHERE id = ?",
            (current_amount, time.time(), goal_id),
        )
        conn.commit()
        conn.close()

    def deactivate_goal(self, goal_id: int) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE goals SET active = 0, updated_at = ? WHERE id = ?",
            (time.time(), goal_id),
        )
        conn.commit()
        conn.close()

    # ── onboarding ───────────────────────────────────────────────────────────

    _ONBOARDING_FIELDS = (
        "income", "income_day", "fixed_costs", "savings_monthly",
        "savings_purpose", "savings_target", "savings_deadline", "budget_style",
    )

    def get_onboarding(self, agent_id: str = "finance") -> OnboardingDTO | None:
        conn = self._db()
        row = conn.execute(
            "SELECT stage, income, income_day, fixed_costs, savings_monthly, savings_purpose, "
            "savings_target, savings_deadline, budget_style, completed_at, updated_at "
            "FROM onboarding WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        fixed_costs = None
        if row[3]:
            try:
                fixed_costs = json.loads(row[3])
            except (TypeError, ValueError):
                fixed_costs = row[3]
        return OnboardingDTO(
            stage=row[0],
            income=row[1],
            income_day=row[2],
            fixed_costs=fixed_costs,
            savings_monthly=row[4],
            savings_purpose=row[5],
            savings_target=row[6],
            savings_deadline=row[7],
            budget_style=row[8],
            completed_at=row[9],
            updated_at=row[10],
        )

    def set_onboarding_stage(self, agent_id: str, stage: str, **kwargs: Any) -> None:
        invalid = set(kwargs) - set(self._ONBOARDING_FIELDS)
        if invalid:
            raise ValueError(f"Unknown onboarding field(s): {', '.join(sorted(invalid))}")
        if "fixed_costs" in kwargs and not isinstance(kwargs["fixed_costs"], str):
            kwargs["fixed_costs"] = json.dumps(kwargs["fixed_costs"], ensure_ascii=False)

        existing_dto = self.get_onboarding(agent_id)
        existing: dict[str, Any] = (
            existing_dto.model_dump() if existing_dto is not None else {"budget_style": "honest"}
        )
        existing.update(kwargs)

        now = time.time()
        conn = self._db()
        conn.execute(
            "INSERT INTO onboarding "
            "(agent_id, stage, income, income_day, fixed_costs, savings_monthly, "
            " savings_purpose, savings_target, savings_deadline, budget_style, completed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "  stage=excluded.stage, income=excluded.income, income_day=excluded.income_day, "
            "  fixed_costs=excluded.fixed_costs, savings_monthly=excluded.savings_monthly, "
            "  savings_purpose=excluded.savings_purpose, savings_target=excluded.savings_target, "
            "  savings_deadline=excluded.savings_deadline, budget_style=excluded.budget_style, "
            "  updated_at=excluded.updated_at",
            (
                agent_id, stage,
                existing.get("income"), existing.get("income_day"), existing.get("fixed_costs"),
                existing.get("savings_monthly"), existing.get("savings_purpose"),
                existing.get("savings_target"), existing.get("savings_deadline"),
                existing.get("budget_style", "honest"), existing.get("completed_at"), now,
            ),
        )
        conn.commit()
        conn.close()

    def complete_onboarding(self, agent_id: str) -> None:
        now = time.time()
        conn = self._db()
        conn.execute(
            "UPDATE onboarding SET completed_at = ?, updated_at = ? WHERE agent_id = ?",
            (now, now, agent_id),
        )
        conn.commit()
        conn.close()

    def reset_onboarding(self, agent_id: str) -> None:
        conn = self._db()
        conn.execute("DELETE FROM onboarding WHERE agent_id = ?", (agent_id,))
        conn.commit()
        conn.close()

    # ── recurring charges ────────────────────────────────────────────────────

    def get_recurring_candidates(self, lookback_days: int = 180, min_count: int = 3) -> list[RecurringCandidateDTO]:
        """
        Return candidate merchants for recurring-charge classification:
        every debit merchant/currency pair with >= min_count charges in the
        lookback window, with the raw chronological (date, amount) sequence
        — the classification logic in server.py needs the actual sequence
        to detect price-change runs, not just aggregate stats.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        conn = self._db()
        rows = conn.execute(
            "SELECT t.date, t.amount, t.currency, t.category_top, t.category_leaf, "
            "       COALESCE(NULLIF(m.resolved_name, ''), t.description) AS merchant "
            "FROM transactions t "
            "LEFT JOIN merchants m ON t.description = m.raw_name "
            "WHERE t.direction != 'CRDT' AND t.date >= ? "
            "ORDER BY merchant, t.currency, t.date ASC",
            (cutoff,),
        ).fetchall()
        conn.close()

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for date, amount, currency, category_top, category_leaf, merchant in rows:
            key = (merchant, currency)
            g = groups.setdefault(
                key, {"merchant": merchant, "currency": currency, "charges": [], "categories": []}
            )
            g["charges"].append((date, amount))
            g["categories"].append(category_leaf or category_top)

        return [
            RecurringCandidateDTO(
                merchant=g["merchant"], currency=g["currency"], charges=g["charges"], categories=g["categories"]
            )
            for g in groups.values()
            if len(g["charges"]) >= min_count
        ]

    def get_recurring_statuses(self) -> dict[tuple[str, str], RecurringStatusDTO]:
        """Bulk-read all recorded cancellation-confirmation statuses, keyed
        (merchant, currency). One query — callers merge in Python rather
        than querying per-merchant."""
        conn = self._db()
        rows = conn.execute(
            "SELECT merchant, currency, status, confirmed_at FROM recurring_charge_status"
        ).fetchall()
        conn.close()
        return {
            (merchant, currency): RecurringStatusDTO(status=status, confirmed_at=confirmed_at)
            for merchant, currency, status, confirmed_at in rows
        }

    _RECURRING_STATUSES = {"active", "cancelled", "unknown"}

    def set_recurring_status(self, merchant: str, currency: str, status: str) -> None:
        if status not in self._RECURRING_STATUSES:
            raise ValueError(
                f"Invalid status: {status!r}. Must be one of {sorted(self._RECURRING_STATUSES)}"
            )
        conn = self._db()
        conn.execute(
            "INSERT OR REPLACE INTO recurring_charge_status "
            "(merchant, currency, status, confirmed_at) VALUES (?, ?, ?, ?)",
            (merchant, currency, status, time.time()),
        )
        conn.commit()
        conn.close()

    # ── budget history ───────────────────────────────────────────────────────

    def record_budget_history(
        self,
        agent_id: str,
        category_top: str,
        period: str,
        limit_amount: float,
        actual_amount: float,
    ) -> None:
        variance = round(actual_amount - limit_amount, 2)
        conn = self._db()
        conn.execute(
            "INSERT INTO budget_history "
            "(agent_id, category_top, period, limit_amount, actual_amount, variance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, category_top, period, limit_amount, actual_amount, variance, time.time()),
        )
        conn.commit()
        conn.close()

    def get_budget_history(self, agent_id: str, category_top: str, n_periods: int = 3) -> list[BudgetHistoryEntryDTO]:
        conn = self._db()
        rows = conn.execute(
            "SELECT period, limit_amount, actual_amount, variance, created_at, "
            "       strftime('%Y-%m', datetime(created_at, 'unixepoch')) AS month "
            "FROM budget_history "
            "WHERE agent_id = ? AND category_top = ? "
            "ORDER BY created_at DESC",
            (agent_id, category_top),
        ).fetchall()
        conn.close()

        # Recorded nightly, so a single calendar month has many rows — keep
        # only the most recent (highest created_at) snapshot per month.
        seen_months: set[str] = set()
        result: list[BudgetHistoryEntryDTO] = []
        for period, limit_amount, actual_amount, variance, created_at, month in rows:
            if month in seen_months:
                continue
            seen_months.add(month)
            result.append(BudgetHistoryEntryDTO(
                month=month, period=period, limit_amount=limit_amount,
                actual_amount=actual_amount, variance=variance, created_at=created_at,
            ))
            if len(result) >= n_periods:
                break
        return result

    def get_overspend_patterns(self, agent_id: str, consecutive_months: int = 3) -> list[OverspendPatternDTO]:
        conn = self._db()
        categories = conn.execute(
            "SELECT DISTINCT category_top FROM budget_history WHERE agent_id = ?",
            (agent_id,),
        ).fetchall()
        conn.close()

        patterns: list[OverspendPatternDTO] = []
        for (category_top,) in categories:
            history = self.get_budget_history(agent_id, category_top, n_periods=consecutive_months)
            if len(history) < consecutive_months:
                continue
            if all(h.variance > 0 for h in history):
                patterns.append(OverspendPatternDTO(
                    category_top=category_top,
                    consecutive_months=consecutive_months,
                    months=[h.month for h in history],
                    avg_variance=round(sum(h.variance for h in history) / len(history), 2),
                ))
        return patterns

    # ── tips ─────────────────────────────────────────────────────────────────

    _TIP_FEEDBACK_STATUSES = {"evaluated", "accepted", "rejected"}
    _TIP_FEEDBACK_REASON_CODES = {
        "not_representative", "already_addressed", "not_actionable",
        "inaccurate", "not_relevant", "other",
    }

    def create_tip(
        self,
        tip_date: str,
        window_from: str,
        window_to: str,
        tip_text: str,
        subject_key: str | None,
        category_top: str | None,
        based_on: str | None,
        signals_json: str,
        model: str,
        prompt_version: str,
        agent_id: str = "finance",
    ) -> int:
        """INSERT a new tip row, returning its id. UNIQUE(agent_id, tip_date)
        is the DB-level backstop against duplicates — the caller (jyske_mcp/slices/finance/tips.py)
        already checks get_tip_for_date first, but this raises
        sqlite3.IntegrityError instead of silently duplicating if that guard
        is ever bypassed or racing."""
        conn = self._db()
        cur = conn.execute(
            "INSERT INTO tips "
            "(agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            " subject_key, category_top, based_on, signals_json, model, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id, time.time(), tip_date, window_from, window_to, tip_text,
                subject_key, category_top, based_on, signals_json, model, prompt_version,
            ),
        )
        tip_id = cur.lastrowid
        conn.commit()
        conn.close()
        assert tip_id is not None  # always set after an INSERT
        return tip_id

    def get_tip_for_date(self, tip_date: str, agent_id: str = "finance") -> TipDTO | None:
        conn = self._db()
        row = conn.execute(
            "SELECT id, agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            "subject_key, category_top, based_on, signals_json, model, prompt_version, "
            "feedback_status, feedback_reason_code, feedback_reason_text, feedback_source, feedback_at "
            "FROM tips WHERE agent_id = ? AND tip_date = ?",
            (agent_id, tip_date),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return self._tip_row_to_dto(row)

    def get_recent_tips_with_feedback(self, n: int = 10, agent_id: str = "finance") -> list[TipDTO]:
        conn = self._db()
        rows = conn.execute(
            "SELECT id, agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            "subject_key, category_top, based_on, signals_json, model, prompt_version, "
            "feedback_status, feedback_reason_code, feedback_reason_text, feedback_source, feedback_at "
            "FROM tips WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, n),
        ).fetchall()
        conn.close()
        return [self._tip_row_to_dto(row) for row in rows]

    def get_rejected_subject_keys(self, since_days: int = 30, agent_id: str = "finance") -> set[str]:
        cutoff = time.time() - since_days * 86400
        conn = self._db()
        rows = conn.execute(
            "SELECT DISTINCT subject_key FROM tips "
            "WHERE agent_id = ? AND feedback_status = 'rejected' "
            "AND subject_key IS NOT NULL AND created_at >= ?",
            (agent_id, cutoff),
        ).fetchall()
        conn.close()
        return {row[0] for row in rows}

    def set_tip_feedback(
        self,
        tip_id: int,
        feedback_status: str,
        reason_code: str | None,
        reason_text: str | None,
        source: str,
    ) -> None:
        if feedback_status not in self._TIP_FEEDBACK_STATUSES:
            raise ValueError(
                f"Invalid feedback_status: {feedback_status!r}. "
                f"Must be one of {sorted(self._TIP_FEEDBACK_STATUSES)}"
            )
        if reason_code is not None and reason_code not in self._TIP_FEEDBACK_REASON_CODES:
            raise ValueError(
                f"Invalid reason_code: {reason_code!r}. "
                f"Must be one of {sorted(self._TIP_FEEDBACK_REASON_CODES)}"
            )
        conn = self._db()
        conn.execute(
            "UPDATE tips SET feedback_status = ?, feedback_reason_code = ?, "
            "feedback_reason_text = ?, feedback_source = ?, feedback_at = ? "
            "WHERE id = ?",
            (feedback_status, reason_code, reason_text, source, time.time(), tip_id),
        )
        conn.commit()
        conn.close()

    def get_labeled_tips(self, agent_id: str = "finance") -> list[dict[str, Any]]:
        """Eval-set export query: every tip with feedback recorded, oldest
        first. No caller needs this yet — it exists so accumulated tips +
        feedback can be exported as an evaluation dataset later. Returns a
        narrower column subset than TipDTO (no id/agent_id/created_at/
        tip_date/subject_key/category_top/feedback_source/feedback_at), so
        it stays a plain dict rather than misrepresenting TipDTO's shape."""
        conn = self._db()
        rows = conn.execute(
            "SELECT tip_text, signals_json, based_on, window_from, window_to, model, "
            "prompt_version, feedback_status, feedback_reason_code, feedback_reason_text "
            "FROM tips WHERE agent_id = ? AND feedback_status != 'pending' ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [
            {
                "tip_text":             r[0],
                "signals_json":         r[1],
                "based_on":             r[2],
                "window_from":          r[3],
                "window_to":            r[4],
                "model":                r[5],
                "prompt_version":       r[6],
                "feedback_status":      r[7],
                "feedback_reason_code": r[8],
                "feedback_reason_text": r[9],
            }
            for r in rows
        ]

    def get_all_tips_with_feedback(self, agent_id: str = "finance") -> list[TipDTO]:
        """Every tip row for the agent, any feedback_status (including
        pending), full columns — unlike get_labeled_tips() (feedback-only
        subset of columns, excludes pending) or get_recent_tips_with_feedback()
        (capped at n). Used by /audit/data for a complete, unpaginated dump."""
        conn = self._db()
        rows = conn.execute(
            "SELECT id, agent_id, created_at, tip_date, window_from, window_to, tip_text, "
            "subject_key, category_top, based_on, signals_json, model, prompt_version, "
            "feedback_status, feedback_reason_code, feedback_reason_text, feedback_source, feedback_at "
            "FROM tips WHERE agent_id = ? ORDER BY created_at",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [self._tip_row_to_dto(row) for row in rows]

    @staticmethod
    def _tip_row_to_dto(row: Any) -> TipDTO:
        return TipDTO(
            id=row[0],
            agent_id=row[1],
            created_at=row[2],
            tip_date=row[3],
            window_from=row[4],
            window_to=row[5],
            tip_text=row[6],
            subject_key=row[7],
            category_top=row[8],
            based_on=row[9],
            signals_json=row[10],
            model=row[11],
            prompt_version=row[12],
            feedback_status=row[13],
            feedback_reason_code=row[14],
            feedback_reason_text=row[15],
            feedback_source=row[16],
            feedback_at=row[17],
        )


class Storage(FinanceStorage):
    """The combined kernel + finance storage surface. Every current finance/
    platform caller imports this single class
    (`from jyske_mcp.slices.finance.storage import Storage`) and calls
    kernel and finance methods on one instance. Deliverable #9 threads
    agent_id end-to-end; until then this remains the single instantiation
    point for callers that haven't been split into kernel-only vs
    finance-only use (mcp/server.py, web/app.py, jobs/tips.py,
    jobs/scheduler.py, scripts/*). Kernel-only callers (kernel/llm.py,
    kernel/sync.py) use KernelStorage directly instead, per
    .agent/epics/vsa-restructure-blueprint.md §1/§7."""

    pass
