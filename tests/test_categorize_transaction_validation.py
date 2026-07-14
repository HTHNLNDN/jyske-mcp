"""
Covers the categorize_transaction MCP tool's (jyske_mcp/slices/finance/tools.py)
validation of the llm_category free-text path ("Top > Mid > Leaf") against
validate_category_pair() (kernel/categorizer.py). This is an interactive
chat-tool call — the calling Claude can retry in the same turn — so an
invalid top/mid returns an error string instead of being silently coerced
(contrast with kernel/sync.py's _batch_categorize, an unattended job that
does coerce — see tests/jobs/test_sync_batch_categorize_validation.py).

v1's shipped categories.json (see .agent/epics/custom-categorization.md)
has no real mid-level content — every top maps to an empty list; only the
top/mid *mechanism* is kept in place, for a future deliverable. Where a
test needs a genuinely valid (top, mid) pair, it monkeypatches one in
under a real top rather than asserting against content that doesn't exist
yet.

Uses the full_schema_storage fixture (tests/conftest.py) and monkeypatches
it onto tools.py's module-level `storage` global, since tools.py builds
that instance once at import time before any fixture can redirect the
kernel storage globals it reads from.
"""
import jyske_mcp.kernel.categorizer as categorizer
import jyske_mcp.slices.finance.tools as tools


def _inject_mid(monkeypatch, top: str, mids: list[str]) -> None:
    """Force the real categories.json to be loaded first (so this doesn't
    depend on some other test module having already triggered it), then
    override one top's mid list."""
    categorizer.category_tree()
    monkeypatch.setitem(categorizer._category_tree, top, mids)


def test_valid_llm_category_is_stored(full_schema_storage, monkeypatch):
    monkeypatch.setattr(tools, "storage", full_schema_storage)
    _inject_mid(monkeypatch, "Eating Out", ["Restaurants"])

    result = tools.categorize_transaction("Cafe X", llm_category="Eating Out > Restaurants > Cafe")

    assert result == "Eating Out > Restaurants > Cafe  (stored, source=llm)"
    stored = full_schema_storage.merchant_get("Cafe X")
    assert stored.category_top == "Eating Out"
    assert stored.category_mid == "Restaurants"
    assert stored.category_leaf == "Cafe"


def test_invalid_top_returns_validate_category_error_and_does_not_store(full_schema_storage, monkeypatch):
    monkeypatch.setattr(tools, "storage", full_schema_storage)

    result = tools.categorize_transaction(
        "Weird Shop", llm_category="Not A Real Category > Whatever > Whatever"
    )

    # Reuses _validate_category()'s existing error-string shape — don't
    # duplicate that message shape with a different one.
    assert "Unknown category 'Not A Real Category'" in result
    assert "Valid categories:" in result
    assert full_schema_storage.merchant_get("Weird Shop") is None


def test_invalid_mid_returns_error_listing_valid_mids_and_does_not_store(full_schema_storage, monkeypatch):
    monkeypatch.setattr(tools, "storage", full_schema_storage)
    # "Eating Out" is a real top, but v1's categories.json has no
    # mid-level content — inject a mid so the "valid mids listed" half of
    # this assertion has something real to list.
    _inject_mid(monkeypatch, "Eating Out", ["Restaurants"])

    # "Restaurants & Cafes" is the exact real-world mislabel this whole
    # fix targets — never a real mid under Eating Out.
    result = tools.categorize_transaction(
        "Bistro Y", llm_category="Eating Out > Restaurants & Cafes > Bistro"
    )

    assert "Unknown category_mid 'Restaurants & Cafes' for 'Eating Out'" in result
    assert "Restaurants" in result  # one of the valid mids is listed
    assert full_schema_storage.merchant_get("Bistro Y") is None


def test_blank_mid_is_allowed_and_stored_as_given(full_schema_storage, monkeypatch):
    monkeypatch.setattr(tools, "storage", full_schema_storage)

    result = tools.categorize_transaction("Mystery Merchant", llm_category="Transfers & Other >  > Uncategorized")

    assert "(stored, source=llm)" in result
    stored = full_schema_storage.merchant_get("Mystery Merchant")
    assert stored.category_top == "Transfers & Other"
    assert stored.category_mid == ""
    assert stored.category_leaf == "Uncategorized"
