"""
Unit tests for jyske_mcp.mcp.server._segment_price_runs (and the
_segment_runs/_drop_interior_noise/_merge_adjacent_runs pipeline it composes)
— the price-run segmentation used by recurring-charge classification to
tolerate a one-time subscription price change without destroying the whole
history's "stability", while folding a single stray noise charge into its
surrounding run instead of treating it as a real price change.

Each charge is a (date, amount) tuple, chronologically ordered, exactly as
Storage.get_recurring_candidates() produces them.
"""
from jyske_mcp.mcp.server import (
    _segment_price_runs,
    _drop_interior_noise,
    _merge_adjacent_runs,
)


def test_empty_charges_yields_no_runs():
    assert _segment_price_runs([]) == []


def test_stable_price_yields_one_run():
    charges = [
        ("2026-01-01", 100.0),
        ("2026-02-01", 100.0),
        ("2026-03-01", 101.0),  # within AMOUNT_TOLERANCE (5%) of median
        ("2026-04-01", 99.0),
    ]
    runs = _segment_price_runs(charges)

    assert len(runs) == 1
    assert len(runs[0]["amounts"]) == 4
    assert runs[0].get("absorbed", []) == []


def test_price_change_mid_run_segments_into_two_runs():
    charges = [
        ("2026-01-01", 100.0),
        ("2026-02-01", 100.0),
        ("2026-03-01", 100.0),
        ("2026-04-01", 150.0),  # a real, sustained price increase
        ("2026-05-01", 150.0),
        ("2026-06-01", 150.0),
    ]
    runs = _segment_price_runs(charges)

    assert len(runs) == 2
    assert runs[0]["amounts"] == [100.0, 100.0, 100.0]
    assert runs[1]["amounts"] == [150.0, 150.0, 150.0]


def test_interior_noise_charge_is_absorbed_not_a_new_run():
    charges = [
        ("2026-01-01", 100.0),
        ("2026-02-01", 100.0),
        ("2026-03-01", 100.0),
        ("2026-04-01", 250.0),  # a single stray outlier charge
        ("2026-05-01", 100.0),
        ("2026-06-01", 100.0),
        ("2026-07-01", 100.0),
    ]
    runs = _segment_price_runs(charges)

    # The noise charge doesn't survive as its own run, and the two
    # surrounding same-price runs re-merge once it's dropped (see
    # test_adjacent_equal_runs_are_merged_after_noise_drop below).
    assert len(runs) == 1
    assert len(runs[0]["amounts"]) == 6
    assert runs[0]["absorbed"] == [("2026-04-01", 250.0)]


def test_adjacent_equal_runs_are_merged_after_noise_drop():
    # Directly exercise the merge step in isolation: two runs at the same
    # price, adjacent after noise-dropping, must combine into one run
    # (amounts, dates, and absorbed lists all concatenated).
    runs = [
        {"amounts": [100.0, 100.0], "dates": ["2026-01-01", "2026-02-01"],
         "absorbed": [("2026-02-15", 250.0)]},
        {"amounts": [101.0, 100.0], "dates": ["2026-03-01", "2026-04-01"]},
    ]
    merged = _merge_adjacent_runs(runs)

    assert len(merged) == 1
    assert merged[0]["amounts"] == [100.0, 100.0, 101.0, 100.0]
    assert merged[0]["dates"] == ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
    assert merged[0]["absorbed"] == [("2026-02-15", 250.0)]


def test_adjacent_runs_with_different_price_are_not_merged():
    runs = [
        {"amounts": [100.0, 100.0], "dates": ["2026-01-01", "2026-02-01"]},
        {"amounts": [150.0, 150.0], "dates": ["2026-03-01", "2026-04-01"]},
    ]
    merged = _merge_adjacent_runs(runs)

    assert len(merged) == 2


def test_drop_interior_noise_leaves_short_run_list_untouched():
    # _drop_interior_noise only acts when there's a genuine "interior" run
    # (i.e. at least 3 runs total) -- with fewer than that there's nothing
    # to fold, so the runs must pass through unchanged.
    runs = [
        {"amounts": [100.0], "dates": ["2026-01-01"]},
        {"amounts": [200.0], "dates": ["2026-02-01"]},
    ]
    assert _drop_interior_noise(runs) == runs


def test_interior_run_at_or_above_min_run_len_is_kept_as_its_own_run():
    # An interior run of length >= MIN_RUN_LEN (2) is a real, established
    # price change of its own -- not noise -- so it must survive as a
    # distinct run rather than being folded into its neighbor.
    charges = [
        ("2026-01-01", 100.0),
        ("2026-02-01", 100.0),
        ("2026-03-01", 100.0),
        ("2026-04-01", 175.0),
        ("2026-05-01", 175.0),  # 2 charges at 175 -- an established run
        ("2026-06-01", 100.0),
        ("2026-07-01", 100.0),
        ("2026-08-01", 100.0),
    ]
    runs = _segment_price_runs(charges)

    assert len(runs) == 3
    assert [r["amounts"] for r in runs] == [
        [100.0, 100.0, 100.0],
        [175.0, 175.0],
        [100.0, 100.0, 100.0],
    ]
