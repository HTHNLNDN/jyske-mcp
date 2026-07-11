"""
Recurring-charge classification — the trickiest piece of the finance slice's
aggregation surface: distinguishing "fixed recurring" charges (same price,
regular cadence — a subscription) from "frequent merchant" spend (regular
cadence or high frequency, but a variable amount — groceries, usage-billed
utilities) while tolerating a one-time price change on a subscription
without breaking its classification. See _classify_recurring.

Relocated out of jyske_mcp/mcp/server.py at epic deliverable #7a
(.agent/epics/vsa-restructure-blueprint.md §4) — behavior-preserving move,
no logic changes. Used by tools.py's recurring_charges tool.
"""

import statistics
from collections import Counter
from datetime import datetime, timezone, timedelta

AMOUNT_TOLERANCE = 0.05          # 5% — how close two charge amounts must be to count as "the same price"
MIN_RUN_LEN = 2                  # a price must repeat at least this many times to count as "established"
GAP_REGULARITY_MAX_CV = 0.25     # max coefficient of variation (stdev/mean) of gaps to call cadence "regular"
FREQUENT_MIN_COUNT = 6           # charge count threshold for "frequent merchant" (independent of cadence)
FREQUENT_MAX_MEAN_GAP = 14       # average gap (days) threshold for "frequent merchant"
STALE_FACTOR = 2                 # a merchant is "stale" once the gap since last charge exceeds STALE_FACTOR * gap_median

# (bucket name, expected gap in days, ± tolerance in days)
_CADENCE_BUCKETS = [
    ("weekly",    7,   2),
    ("biweekly",  14,  3),
    ("monthly",   30,  6),
    ("quarterly", 91,  12),
    ("annual",    365, 30),
]


def _date_gaps(dates: list[str]) -> list[int]:
    parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    return [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]


def _match_cadence(gap_median: float | None) -> str | None:
    """Nearest cadence bucket whose tolerance band contains gap_median, or None."""
    if gap_median is None:
        return None
    candidates = [
        (name, abs(gap_median - center))
        for name, center, tol in _CADENCE_BUCKETS
        if abs(gap_median - center) <= tol
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[1])[0]


def _segment_runs(charges: list[tuple[str, float]]) -> list[dict]:
    """
    Walk charges chronologically, grouping consecutive charges into "runs" of
    a stable price. A charge joins the current run if it's within
    AMOUNT_TOLERANCE of that run's *running median* (recomputed on every
    append); otherwise it starts a new run. This is what lets a subscription
    price increase show up as two runs instead of destroying the whole
    history's "stability".
    """
    runs: list[dict] = []
    amounts: list[float] = []
    dates: list[str] = []
    for date, amount in charges:
        if not amounts:
            amounts, dates = [amount], [date]
            continue
        median = statistics.median(amounts)
        within_tolerance = (
            abs(amount - median) <= AMOUNT_TOLERANCE * abs(median)
            if median else amount == 0
        )
        if within_tolerance:
            amounts.append(amount)
            dates.append(date)
        else:
            runs.append({"amounts": amounts, "dates": dates})
            amounts, dates = [amount], [date]
    if amounts:
        runs.append({"amounts": amounts, "dates": dates})
    return runs


def _drop_interior_noise(runs: list[dict]) -> list[dict]:
    """Fold interior runs (bounded by a run on both sides) shorter than
    MIN_RUN_LEN into the preceding kept run — single stray charges that don't
    represent a real price change, just noise between two stretches of a
    stable price.

    These charges are *not* discarded: every charge must land in exactly one
    run. They're tracked in a separate "absorbed" list (date, amount) rather
    than merged into "amounts"/"dates" so a handful of outlier noise charges
    can't skew the run's median/typical price — only its count and date
    span."""
    if len(runs) < 3:
        return runs
    kept = [dict(runs[0])]
    for i in range(1, len(runs) - 1):
        if len(runs[i]["amounts"]) >= MIN_RUN_LEN:
            kept.append(dict(runs[i]))
        else:
            prev = kept[-1]
            prev["absorbed"] = prev.get("absorbed", []) + list(
                zip(runs[i]["dates"], runs[i]["amounts"])
            )
    kept.append(dict(runs[-1]))
    return kept


def _merge_adjacent_runs(runs: list[dict]) -> list[dict]:
    """After dropping interior noise, runs that are now adjacent may again be
    within tolerance of each other (the noise run in between was the only
    thing separating them) — merge those back together."""
    if not runs:
        return runs
    merged = [dict(runs[0])]
    for run in runs[1:]:
        prev = merged[-1]
        prev_median = statistics.median(prev["amounts"])
        run_median = statistics.median(run["amounts"])
        same_price = (
            abs(run_median - prev_median) <= AMOUNT_TOLERANCE * abs(prev_median)
            if prev_median else run_median == 0
        )
        if same_price:
            prev["amounts"] = prev["amounts"] + run["amounts"]
            prev["dates"] = prev["dates"] + run["dates"]
            prev["absorbed"] = prev.get("absorbed", []) + run.get("absorbed", [])
        else:
            merged.append(dict(run))
    return merged


def _run_charge_count(run: dict) -> int:
    """Total real charges represented by a run: the price-defining charges
    plus any interior-noise charges absorbed into it (see
    _drop_interior_noise). Always use this instead of len(run["amounts"])
    when reporting counts — the latter undercounts by the absorbed noise."""
    return len(run["amounts"]) + len(run.get("absorbed", []))


def _run_date_span(run: dict) -> tuple[str, str]:
    """(first_seen, last_seen) across a run's price-defining charges and any
    absorbed noise charges — computed via min/max rather than list
    positions, since absorbed dates aren't necessarily interleaved in
    chronological order within the run's date list."""
    all_dates = run["dates"] + [d for d, _ in run.get("absorbed", [])]
    return min(all_dates), max(all_dates)


def _segment_price_runs(charges: list[tuple[str, float]]) -> list[dict]:
    runs = _segment_runs(charges)
    runs = _drop_interior_noise(runs)
    runs = _merge_adjacent_runs(runs)
    return runs


def _established_runs(charges: list[tuple[str, float]]) -> tuple[list[dict], bool]:
    """
    Returns (established_runs, current_run_stable).

    established_runs are price-history runs oldest→newest, excluding a
    trailing run that's too short to "count" yet (a single new-looking charge
    that hasn't repeated — see MIN_RUN_LEN). The previous run is used as the
    "current" price in that case.

    current_run_stable is False only in the degenerate fallback case where
    segmentation couldn't find *any* run of length >= MIN_RUN_LEN — i.e. the
    amount genuinely varies charge to charge (groceries, usage-billed
    utilities) rather than settling on a price. True in every other case,
    including when a too-new trailing charge was excluded.
    """
    runs = _segment_price_runs(charges)
    if not runs:
        return [], False  # shouldn't happen — MIN_COUNT already passed upstream

    if len(runs[-1]["amounts"]) >= MIN_RUN_LEN:
        return runs, True

    established = runs[:-1]
    if established and len(established[-1]["amounts"]) >= MIN_RUN_LEN:
        return established, True

    # Degenerate fallback: collapse to one whole-history run so downstream
    # code always has something to read typical_amount/price_history off of,
    # but flag it unstable so fixed_recurring isn't (mis)claimed for data
    # that's actually just noisy/variable.
    all_amounts = [a for run in runs for a in run["amounts"]]
    all_dates = [d for run in runs for d in run["dates"]]
    all_absorbed = [pair for run in runs for pair in run.get("absorbed", [])]
    return [{"amounts": all_amounts, "dates": all_dates, "absorbed": all_absorbed}], False


def _classify_recurring(
    candidate: dict,
    statuses: dict[tuple[str, str], dict],
    today: datetime,
    lookback_days: int,
    min_count: int,
) -> dict | None:
    """Classify a single merchant candidate (see Storage.get_recurring_candidates).
    Returns None if the merchant is neither fixed_recurring nor frequent_merchant."""
    merchant = candidate["merchant"]
    currency = candidate["currency"]
    charges = candidate["charges"]  # [(date, amount), ...] sorted ascending
    count = len(charges)
    if count < min_count:
        return None

    dates = [d for d, _ in charges]
    gaps = _date_gaps(dates)
    if gaps:
        gap_mean = statistics.mean(gaps)
        gap_median = statistics.median(gaps)
        gap_stdev = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
        gap_cv = (gap_stdev / gap_mean) if gap_mean else None
    else:
        gap_mean = gap_median = gap_stdev = gap_cv = None

    cadence = _match_cadence(gap_median)
    cadence_regular = cadence is not None and gap_cv is not None and gap_cv <= GAP_REGULARITY_MAX_CV

    established, current_run_stable = _established_runs(charges)
    typical_amount = round(statistics.median(established[-1]["amounts"]), 2)
    price_history = []
    for run in established:
        first_seen_run, last_seen_run = _run_date_span(run)
        price_history.append({
            "amount":     round(statistics.median(run["amounts"]), 2),
            "first_seen": first_seen_run,
            "last_seen":  last_seen_run,
            "count":      _run_charge_count(run),
        })

    is_frequent = count >= FREQUENT_MIN_COUNT and gap_mean is not None and gap_mean <= FREQUENT_MAX_MEAN_GAP

    kinds = []
    if count >= min_count and cadence_regular and current_run_stable:
        kinds.append("fixed_recurring")
    if is_frequent or (cadence_regular and not current_run_stable and count >= min_count):
        kinds.append("frequent_merchant")
    if not kinds:
        return None
    primary_kind = "fixed_recurring" if "fixed_recurring" in kinds else "frequent_merchant"

    first_seen, last_seen = dates[0], dates[-1]
    last_seen_dt = datetime.strptime(last_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    first_seen_dt = datetime.strptime(first_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    stale = gap_median is not None and (today - last_seen_dt).days > STALE_FACTOR * gap_median

    conf = statuses.get((merchant, currency))
    covered = conf is not None and conf["confirmed_at"] >= last_seen_dt.timestamp()

    if conf and conf["status"] == "cancelled" and covered:
        status = "cancelled_confirmed"
    elif conf and conf["status"] == "active" and covered:
        status = "active"
    elif stale:
        status = "inactive_unconfirmed"
    else:
        status = "active"
    needs_confirmation = stale and not covered

    next_expected = None
    if cadence is not None and gap_median is not None:
        next_expected = (last_seen_dt + timedelta(days=gap_median)).strftime("%Y-%m-%d")

    total_in_window = round(sum(a for _, a in charges), 2)
    if cadence is not None and gap_median:
        monthly_estimate = round(typical_amount * (30 / gap_median), 2)
    else:
        span_days = (last_seen_dt - first_seen_dt).days
        denom_days = span_days if span_days > 0 else lookback_days
        monthly_estimate = round(total_in_window / (denom_days / 30), 2)

    cat_counts = Counter(c for c in candidate.get("categories", []) if c)
    category = cat_counts.most_common(1)[0][0] if cat_counts else "Other"

    return {
        "merchant":          merchant,
        "currency":          currency,
        "kinds":             kinds,
        "primary_kind":      primary_kind,
        "category":          category,
        "count":             count,
        "cadence":           cadence,
        "cadence_regular":   cadence_regular,
        "first_seen":        first_seen,
        "last_seen":         last_seen,
        "next_expected":     next_expected,
        "typical_amount":    typical_amount,
        "price_history":     price_history,
        "status":            status,
        "needs_confirmation": needs_confirmation,
        "monthly_estimate":  monthly_estimate,
        "total_in_window":   total_in_window,
    }
