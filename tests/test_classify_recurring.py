"""
Unit tests for jyske_mcp.mcp.server._classify_recurring — classifies a single
merchant candidate (see Storage.get_recurring_candidates) as fixed_recurring
(stable price, regular cadence) and/or frequent_merchant, or returns None.

`candidate` fixtures are hand-built to match get_recurring_candidates()'s
shape: {"merchant", "currency", "charges": [(date, amount), ...] sorted
ascending, "categories": [...]}. `statuses` (recorded cancel/confirm status,
keyed (merchant, currency)) is empty unless a test needs it. `today` is
passed in explicitly (this is a pure function, no wall-clock dependency).
"""
from datetime import datetime, timezone

from jyske_mcp.mcp.server import _classify_recurring


def _candidate(merchant="Netflix", currency="DKK", charges=None, categories=None):
    return {
        "merchant": merchant,
        "currency": currency,
        "charges": charges or [],
        "categories": categories or [],
    }


def test_regular_monthly_cadence_stable_price_is_fixed_recurring():
    charges = [
        ("2026-04-09", 99.0),
        ("2026-05-09", 99.0),
        ("2026-06-08", 99.0),
        ("2026-07-08", 99.0),
    ]
    today = datetime(2026, 7, 9, tzinfo=timezone.utc)  # 1 day after last charge

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=180, min_count=3)

    assert row is not None
    assert "fixed_recurring" in row["kinds"]
    assert row["primary_kind"] == "fixed_recurring"
    assert row["cadence"] == "monthly"
    assert row["cadence_regular"] is True
    assert row["typical_amount"] == 99.0
    assert row["status"] == "active"
    assert row["needs_confirmation"] is False


def test_irregular_gaps_and_variable_amount_is_not_recurring():
    # 4 charges (>= min_count) but gaps are irregular (46, 70, 69 days --
    # none of the cadence buckets match) and there aren't enough of them to
    # qualify as a "frequent_merchant" (needs >= FREQUENT_MIN_COUNT == 6)
    # either -- this is one-off/irregular spend, not a subscription.
    charges = [
        ("2026-01-05", 45.0),
        ("2026-02-20", 120.0),
        ("2026-05-01", 10.0),
        ("2026-07-09", 300.0),
    ]
    today = datetime(2026, 7, 9, tzinfo=timezone.utc)

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=365, min_count=3)

    assert row is None


def test_too_few_charges_is_not_recurring_regardless_of_pattern():
    charges = [("2026-06-01", 50.0), ("2026-07-01", 50.0)]  # perfectly regular, but count < min_count
    today = datetime(2026, 7, 9, tzinfo=timezone.utc)

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=180, min_count=3)

    assert row is None


def test_boundary_cadence_gap_within_weekly_tolerance_is_recognized():
    # Weekly bucket is (7 +/- 2) days -> a 9-day gap is exactly at the edge
    # of the tolerance band and must still be recognized as "weekly".
    charges = [
        ("2026-06-01", 20.0),
        ("2026-06-10", 20.0),
        ("2026-06-19", 20.0),
        ("2026-06-28", 20.0),
    ]
    today = datetime(2026, 6, 28, tzinfo=timezone.utc)

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=90, min_count=3)

    assert row is not None
    assert row["cadence"] == "weekly"
    assert "fixed_recurring" in row["kinds"]


def test_boundary_cadence_gap_just_outside_weekly_and_biweekly_is_not_recurring():
    # A 10-day gap falls in the dead zone between weekly's (5-9 day) and
    # biweekly's (11-17 day) tolerance bands -- no cadence bucket matches,
    # so with only 4 charges (below the frequent_merchant threshold of 6)
    # this must not classify as recurring.
    charges = [
        ("2026-06-01", 20.0),
        ("2026-06-11", 20.0),
        ("2026-06-21", 20.0),
        ("2026-07-01", 20.0),
    ]
    today = datetime(2026, 7, 1, tzinfo=timezone.utc)

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=90, min_count=3)

    assert row is None


def test_frequent_merchant_with_variable_amount_and_no_cadence():
    # High-frequency, short mean gap, but a genuinely variable amount
    # (groceries-style spend) -- classified as frequent_merchant, not
    # fixed_recurring.
    charges = [
        ("2026-06-01", 120.0),
        ("2026-06-05", 340.0),
        ("2026-06-09", 80.0),
        ("2026-06-13", 210.0),
        ("2026-06-17", 95.0),
        ("2026-06-21", 400.0),
    ]
    today = datetime(2026, 6, 21, tzinfo=timezone.utc)

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=90, min_count=3)

    assert row is not None
    assert row["kinds"] == ["frequent_merchant"]
    assert row["primary_kind"] == "frequent_merchant"


def test_stale_merchant_without_confirmation_needs_confirmation():
    # Same regular-monthly pattern as the first test, but "today" is far
    # past the last charge (> STALE_FACTOR * gap_median), and there's no
    # recorded cancellation/active confirmation covering it.
    charges = [
        ("2026-01-09", 99.0),
        ("2026-02-08", 99.0),
        ("2026-03-10", 99.0),
        ("2026-04-09", 99.0),
    ]
    today = datetime(2026, 7, 9, tzinfo=timezone.utc)  # ~91 days after last charge

    row = _classify_recurring(_candidate(charges=charges), {}, today, lookback_days=365, min_count=3)

    assert row is not None
    assert row["status"] == "inactive_unconfirmed"
    assert row["needs_confirmation"] is True
