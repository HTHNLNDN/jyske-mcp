import json
from pathlib import Path

from jyske_mcp.kernel.dto import MerchantCategoryDTO

_CATEGORIES_FILE = Path(__file__).parent / "data" / "categories.json"
_MCC_CODES_FILE = Path(__file__).parent / "data" / "mcc_codes.json"

# Flat map: mcc_code -> (top, mid, leaf)  built once at import time
_mcc_index: dict[str, tuple[str, str, str]] = {}

# Top-level category names, cached the same way as _mcc_index — built once
# at import time from the same categories.json structure.
_top_categories: set[str] = set()

# top -> [mid, ...], built once at import time from the same categories.json
# structure as _mcc_index/_top_categories — single source of truth for the
# category picker (frontend) and server-side recategorization validation.
_category_tree: dict[str, list[str]] = {}


def _load_index() -> None:
    if _mcc_index:
        return

    # Build MCC code → short human-readable description
    descs: dict[str, str] = {}
    for entry in json.loads(_MCC_CODES_FILE.read_text()):
        code = entry.get("mcc", "")
        raw = entry.get("edited_description", "")
        if code and raw:
            # Take the first segment before "–" or "," for a concise leaf label
            descs[code] = raw.split("–")[0].split(",")[0].strip()

    cats = json.loads(_CATEGORIES_FILE.read_text())
    for top, mids in cats.items():
        _top_categories.add(top)
        _category_tree[top] = list(mids.keys())
        for mid, codes in mids.items():
            for code in codes:
                _mcc_index[code] = (top, mid, descs.get(code, mid))


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


def categorize(raw_name: str, mcc: str | None, storage, conn=None) -> MerchantCategoryDTO | None:
    """
    Return a MerchantCategoryDTO or None if LLM categorization is needed.

    Resolution order:
      1. merchants table cache (any source)
      2. MCC lookup in categories.json
      3. None  →  caller must do LLM categorization and store the result

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

    # 2 — MCC lookup
    if mcc:
        hit = _mcc_index.get(mcc)
        if hit:
            top, mid, leaf = hit
            storage.merchant_set(
                raw_name=raw_name,
                category_top=top,
                category_mid=mid,
                category_leaf=leaf,
                mcc=mcc,
                source="mcc_lookup",
                conn=conn,
            )
            return MerchantCategoryDTO(
                category_top=top,
                category_mid=mid,
                category_leaf=leaf,
                resolved_name="",
                mcc=mcc,
                source="mcc_lookup",
            )

    # 3 — signal LLM needed
    return None
