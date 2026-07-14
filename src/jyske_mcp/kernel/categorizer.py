import json
from pathlib import Path

from jyske_mcp.kernel.dto import MerchantCategoryDTO

_CATEGORIES_FILE = Path(__file__).parent / "data" / "categories.json"

# Top-level category names, cached the same way as _category_tree — built
# once at import time from categories.json.
_top_categories: set[str] = set()

# top -> [mid, ...], built once at import time from categories.json —
# single source of truth for the category picker (frontend) and
# server-side recategorization validation. v1's categories.json maps each
# top directly to an empty list (no mid-level content yet); the top/mid
# mechanism itself stays in place for a future deliverable.
_category_tree: dict[str, list[str]] = {}


def _load_index() -> None:
    if _category_tree:
        return

    cats = json.loads(_CATEGORIES_FILE.read_text())
    for top, mids in cats.items():
        _top_categories.add(top)
        _category_tree[top] = list(mids)


def top_categories() -> set[str]:
    """Return the set of valid top-level category names from
    data/categories.json, e.g. for validating a `category` tool argument."""
    _load_index()
    return _top_categories


def category_tree() -> dict[str, list[str]]:
    """Return {top: [mid, ...], ...} from data/categories.json — the single
    source of truth for the category picker (frontend) and for validating
    recategorization requests server-side."""
    _load_index()
    return _category_tree


def validate_category_pair(top: str, mid: str | None) -> tuple[bool, bool]:
    """(top_valid, mid_valid) against category_tree() -- the single source
    of truth every categorization write path and every HTTP validation
    path shares, so "is this category real" can never drift between them.
    mid_valid is True when mid is falsy (no mid claimed) or listed under
    `top`; an invalid top makes mid_valid False regardless of mid."""
    tree = category_tree()
    top_valid = top in tree
    mid_valid = (not mid) or (top_valid and mid in tree[top])
    return top_valid, mid_valid


def categorize(raw_name: str, mcc: str | None, storage, conn=None) -> MerchantCategoryDTO | None:
    """
    Return a MerchantCategoryDTO or None if LLM categorization is needed.

    Resolution order:
      1. merchants table cache (any source)
      2. None  →  caller must do LLM categorization and store the result

    conn: optional borrowed sqlite3 connection, forwarded to
    storage.merchant_get/merchant_set so callers writing inside their own
    transaction (e.g. Storage.store_transaction under WAL) don't open a
    second connection while the first's write transaction is still open.
    """
    _load_index()

    # 1 — merchant cache
    cached = storage.merchant_get(raw_name, conn=conn)
    if cached is not None:
        return cached

    # 2 — signal LLM needed
    return None
