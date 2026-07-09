"""
Unit tests for jyske_mcp.mcp.server._compute_proration — the pure proration
math extracted out of compare_spending (see that function's docstring/
comments). This helper decides whether `month` is the still-in-progress
current calendar month relative to `now`, and if so, what date window
`baseline_month` should be truncated to for an apples-to-apples comparison,
plus the low_confidence flag.

No I/O is involved (no Storage, no temp sqlite) — every case here is a pure
function of (month, baseline_month, now), matching the "test pure helpers
directly with hand-built fixtures" convention.

compare_spending's own behavior (totals/breakdown, including the in-progress
path) is still covered end-to-end by tests/test_mixed_currency_no_blend.py;
this file only isolates the proration math itself.
"""
from datetime import datetime, timezone

from jyske_mcp.mcp.server import _compute_proration


def test_not_in_progress_when_month_is_not_current_calendar_month():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    result = _compute_proration("2026-06", "2026-05", now)

    assert result == {
        "in_progress":            False,
        "pct_elapsed":            None,
        "low_confidence":         False,
        "baseline_prorated_from": None,
        "baseline_prorated_to":   None,
    }


def test_full_elapsed_month_on_last_day():
    # July has 31 days -- on day 31, the whole month has elapsed.
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    result = _compute_proration("2026-07", "2026-06", now)

    assert result["in_progress"] is True
    assert result["pct_elapsed"] == 1.0
    assert result["low_confidence"] is False
    # June only has 30 days, so the window is capped at June's last day, not
    # a nonexistent June 31st.
    assert result["baseline_prorated_from"] == "2026-06-01"
    assert result["baseline_prorated_to"] == "2026-06-30"


def test_partial_month_prorate_day_scales_with_pct_elapsed():
    # Day 10 of a 30-day month -> pct_elapsed == 1/3, and the baseline
    # window (May, 31 days) should track that same elapsed-day count, not
    # the full baseline month.
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    result = _compute_proration("2026-06", "2026-05", now)

    assert result["in_progress"] is True
    assert result["pct_elapsed"] == 10 / 30
    assert result["low_confidence"] is False
    assert result["baseline_prorated_from"] == "2026-05-01"
    assert result["baseline_prorated_to"] == "2026-05-10"

    # The prorated window's fraction of the baseline month roughly tracks
    # the fraction of the current month that has elapsed (they can't be
    # exactly equal since the two months have different lengths).
    prorate_days = int(result["baseline_prorated_to"][-2:])
    baseline_fraction = prorate_days / 31
    assert abs(baseline_fraction - result["pct_elapsed"]) < 1 / 31


def test_day_one_edge_is_low_confidence_and_single_day_window():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    result = _compute_proration("2026-06", "2026-05", now)

    assert result["in_progress"] is True
    assert result["pct_elapsed"] == 1 / 30
    assert result["low_confidence"] is True  # well under the 0.25 threshold
    assert result["baseline_prorated_from"] == "2026-05-01"
    assert result["baseline_prorated_to"] == "2026-05-01"


def test_low_confidence_threshold_boundary():
    # April has 30 days. day 8 -> pct_elapsed == 8/30 (>= 0.25) -> not
    # low_confidence; day 7 -> 7/30 (< 0.25) -> low_confidence. Matches
    # compare_spending's strict `pct_elapsed < 0.25`.
    just_over = _compute_proration("2026-04", "2026-03", datetime(2026, 4, 8, tzinfo=timezone.utc))
    assert just_over["pct_elapsed"] == 8 / 30
    assert just_over["pct_elapsed"] >= 0.25
    assert just_over["low_confidence"] is False

    just_under = _compute_proration("2026-04", "2026-03", datetime(2026, 4, 7, tzinfo=timezone.utc))
    assert just_under["pct_elapsed"] == 7 / 30
    assert just_under["pct_elapsed"] < 0.25
    assert just_under["low_confidence"] is True


def test_baseline_month_shorter_than_elapsed_day_caps_at_baseline_last_day():
    # Elapsed day 30 of a 31-day month, but baseline is February (28 days in
    # 2026, non-leap) -- the prorated window must cap at Feb 28, not overrun
    # into a nonexistent Feb 30.
    now = datetime(2026, 3, 30, tzinfo=timezone.utc)
    result = _compute_proration("2026-03", "2026-02", now)

    assert result["in_progress"] is True
    assert result["baseline_prorated_from"] == "2026-02-01"
    assert result["baseline_prorated_to"] == "2026-02-28"
