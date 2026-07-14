"""
Covers validate_category_pair() (jyske_mcp/kernel/categorizer.py) — the
single source-of-truth taxonomy-validation primitive every categorization
write path (kernel/sync.py's _batch_categorize, the categorize_transaction
MCP tool, /budgets/recategorize, /budgets) shares. See those call sites'
own tests for how each backstop/validation behaves in context; this file
tests the pure function itself against the real categories.json taxonomy.

v1's shipped categories.json (see .agent/epics/custom-categorization.md)
has no real mid-level content — every top maps to an empty list; only the
top/mid *mechanism* is kept in place, for a future deliverable. The "valid
mid" scenarios below monkeypatch a mid in under a real top to prove the
mechanism still works, rather than asserting against content that doesn't
exist yet.
"""
import jyske_mcp.kernel.categorizer as categorizer
from jyske_mcp.kernel.categorizer import validate_category_pair


def _inject_mid(monkeypatch, top: str, mids: list[str]) -> None:
    """Force the real categories.json to be loaded first (so this doesn't
    depend on some other test module having already triggered it), then
    override one top's mid list — proving the top/mid mechanism still
    works without asserting against v1's (empty) real mid content."""
    categorizer.category_tree()
    monkeypatch.setitem(categorizer._category_tree, top, mids)


def test_valid_top_and_valid_mid(monkeypatch):
    _inject_mid(monkeypatch, "Bills", ["Utilities"])

    top_valid, mid_valid = validate_category_pair("Bills", "Utilities")
    assert top_valid is True
    assert mid_valid is True


def test_valid_top_with_no_mid_claimed_is_valid():
    # A falsy mid (None or "") means "no mid claimed" — always valid,
    # regardless of top, as long as top itself is real.
    assert validate_category_pair("Bills", None) == (True, True)
    assert validate_category_pair("Bills", "") == (True, True)


def test_valid_top_with_mid_from_a_different_top_is_invalid(monkeypatch):
    # This is the exact shape of the real bug this primitive fixes: a mid
    # that's real SOMEWHERE, just not under this top.
    _inject_mid(monkeypatch, "Bills", ["Utilities"])
    _inject_mid(monkeypatch, "Transport", ["Fuel"])

    top_valid, mid_valid = validate_category_pair("Bills", "Fuel")
    assert top_valid is True
    assert mid_valid is False


def test_valid_top_with_never_real_mid_is_invalid():
    # v1's real categories.json has no mid-level content at all yet, so
    # any non-blank mid under any real top is currently invalid — this
    # doesn't need a monkeypatch to demonstrate.
    top_valid, mid_valid = validate_category_pair("Bills", "Restaurants & Cafes")
    assert top_valid is True
    assert mid_valid is False


def test_invalid_top_with_a_claimed_mid_is_invalid_regardless_of_mid():
    # An invalid top makes mid_valid False whenever a real mid was claimed
    # (mid truthy) — it can never be "valid under" a top that doesn't exist.
    top_valid, mid_valid = validate_category_pair("Not A Real Category", "Restaurants")
    assert top_valid is False
    assert mid_valid is False


def test_invalid_top_with_no_mid_claimed_short_circuits_mid_valid_true():
    # mid_valid's first clause ("mid is falsy -> no mid claimed") is
    # unconditional -- it doesn't itself check top_valid. This combination
    # (invalid top, no mid claimed at all) is never actually consulted by
    # any real caller, since every call site returns/errors on `not
    # top_valid` before ever looking at mid_valid.
    top_valid, mid_valid = validate_category_pair("Not A Real Category", None)
    assert top_valid is False
    assert mid_valid is True
