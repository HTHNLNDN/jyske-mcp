"""
Pure spending/date math for the finance slice's deterministic aggregation
tools (get_spending, compare_spending, goal_pace — see tools.py). No I/O,
no Storage — every function here is a pure function of its arguments, which
is what lets tests/test_compare_spending_proration.py exercise
_compute_proration directly with hand-built (month, baseline_month, now)
fixtures.

Relocated out of jyske_mcp/mcp/server.py at epic deliverable #7a
(.agent/epics/vsa-restructure-blueprint.md §4) — behavior-preserving move,
no logic changes.
"""

import calendar
from datetime import datetime, timezone


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


def _compute_proration(month: str, baseline_month: str, now: datetime) -> dict:
    """
    Pure proration math for compare_spending: is `month` still the
    in-progress current calendar month relative to `now`, and if so, what
    date window should `baseline_month` be truncated to so it covers the
    same number of elapsed days (an apples-to-apples comparison against a
    full prior calendar month), plus whether so little of the month has
    elapsed that the comparison should be flagged low_confidence.

    No I/O — the caller is responsible for actually summing spend over
    baseline_prorated_from/to (see compare_spending).

    Returns a dict:
      in_progress             bool
      pct_elapsed             float | None  (None when not in_progress)
      low_confidence          bool          (False when not in_progress)
      baseline_prorated_from  str | None
      baseline_prorated_to    str | None
    """
    in_progress = month == now.strftime("%Y-%m")
    if not in_progress:
        return {
            "in_progress":            False,
            "pct_elapsed":            None,
            "low_confidence":         False,
            "baseline_prorated_from": None,
            "baseline_prorated_to":   None,
        }

    year, mon = int(month[:4]), int(month[5:7])
    days_in_month = calendar.monthrange(year, mon)[1]
    day_of_month = now.day
    pct_elapsed = day_of_month / days_in_month
    low_confidence = pct_elapsed < 0.25

    b_year, b_mon = int(baseline_month[:4]), int(baseline_month[5:7])
    baseline_last_day = calendar.monthrange(b_year, b_mon)[1]
    prorate_day = min(day_of_month, baseline_last_day)

    return {
        "in_progress":            True,
        "pct_elapsed":            pct_elapsed,
        "low_confidence":         low_confidence,
        "baseline_prorated_from": f"{baseline_month}-01",
        "baseline_prorated_to":   f"{baseline_month}-{prorate_day:02d}",
    }


def _parse_iso_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None  # deadline is free text, not an ISO date — pacing math can't run on it
