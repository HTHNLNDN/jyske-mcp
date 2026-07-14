"""
Covers the categorization backstop in kernel/sync.py's _batch_categorize():
every LLM result is re-checked with validate_category_pair() (kernel/
categorizer.py) before being cached via storage.merchant_set, so an LLM
ignoring the prompt's closed-taxonomy instructions can never corrupt the
merchants cache with an unknown top or an unmatched mid.

  - An invalid `top` -> the whole row is skipped, nothing cached (left for
    a future LLM pass to categorize this merchant correctly).
  - An invalid `mid` -> coerced to real SQL None (not ""), top/leaf still
    stored.

v1's shipped categories.json (see .agent/epics/custom-categorization.md)
has no real mid-level content — every top maps to an empty list; only the
top/mid *mechanism* is kept in place, for a future deliverable. Where a
test needs a genuinely valid (top, mid) pair, it monkeypatches one in
under a real top rather than asserting against content that doesn't exist
yet.

simple_completion is stubbed out with canned JSON — no live LLM call
anywhere in this file.
"""
from unittest.mock import MagicMock

import jyske_mcp.kernel.categorizer as categorizer
import jyske_mcp.kernel.sync as sync


def _stub_llm(monkeypatch, json_text: str) -> None:
    monkeypatch.setattr(sync, "simple_completion", lambda prompt, model: json_text)


def _inject_mid(monkeypatch, top: str, mids: list[str]) -> None:
    """Force the real categories.json to be loaded first (so this doesn't
    depend on some other test module having already triggered it), then
    override one top's mid list."""
    categorizer.category_tree()
    monkeypatch.setitem(categorizer._category_tree, top, mids)


def test_valid_top_and_mid_stored_as_is(monkeypatch):
    _inject_mid(monkeypatch, "Eating Out", ["Restaurants"])
    _stub_llm(
        monkeypatch,
        '[{"raw_name": "Cafe X", "top": "Eating Out", "mid": "Restaurants", "leaf": "Cafe"}]',
    )
    storage = MagicMock()
    sync._batch_categorize([{"raw_name": "Cafe X", "mcc": None}], storage)

    storage.merchant_set.assert_called_once_with(
        raw_name="Cafe X", category_top="Eating Out", category_mid="Restaurants",
        category_leaf="Cafe", source="llm",
    )


def test_invalid_top_is_never_cached(monkeypatch):
    _stub_llm(
        monkeypatch,
        '[{"raw_name": "Weird Shop", "top": "Not A Real Category", "mid": "Whatever", "leaf": "Whatever"}]',
    )
    storage = MagicMock()
    sync._batch_categorize([{"raw_name": "Weird Shop", "mcc": None}], storage)

    storage.merchant_set.assert_not_called()


def test_invalid_mid_is_coerced_to_none_but_top_and_leaf_still_stored(monkeypatch):
    # "Eating Out" is a real top, but v1's categories.json has no mid-level
    # content under any top (empty list) — any non-blank mid is invalid,
    # no injection needed to demonstrate this.
    _stub_llm(
        monkeypatch,
        '[{"raw_name": "Bistro Y", "top": "Eating Out", "mid": "Restaurants & Cafes", "leaf": "Bistro"}]',
    )
    storage = MagicMock()
    sync._batch_categorize([{"raw_name": "Bistro Y", "mcc": None}], storage)

    storage.merchant_set.assert_called_once_with(
        raw_name="Bistro Y", category_top="Eating Out", category_mid=None,
        category_leaf="Bistro", source="llm",
    )


def test_blank_mid_from_llm_stores_none_without_being_flagged_invalid(monkeypatch):
    # The prompt explicitly allows mid="" when nothing fits — that's "no mid
    # claimed", not an invalid mid, so it stores cleanly as a top-only row.
    _stub_llm(
        monkeypatch,
        '[{"raw_name": "Mystery Merchant", "top": "Transfers & Other", "mid": "", "leaf": "Uncategorized"}]',
    )
    storage = MagicMock()
    sync._batch_categorize([{"raw_name": "Mystery Merchant", "mcc": None}], storage)

    storage.merchant_set.assert_called_once_with(
        raw_name="Mystery Merchant", category_top="Transfers & Other", category_mid=None,
        category_leaf="Uncategorized", source="llm",
    )


def test_mixed_batch_only_skips_the_invalid_top_row(monkeypatch):
    _stub_llm(
        monkeypatch,
        '[{"raw_name": "Good Shop", "top": "Spending Money", "mid": "", "leaf": "Gadgets"},'
        ' {"raw_name": "Bad Shop", "top": "Nonsense", "mid": "Whatever", "leaf": "Whatever"}]',
    )
    storage = MagicMock()
    sync._batch_categorize(
        [{"raw_name": "Good Shop", "mcc": None}, {"raw_name": "Bad Shop", "mcc": None}], storage
    )

    storage.merchant_set.assert_called_once_with(
        raw_name="Good Shop", category_top="Spending Money", category_mid=None,
        category_leaf="Gadgets", source="llm",
    )
