import json
from pathlib import Path

_CATEGORIES_FILE = Path(__file__).parent.parent / "data" / "categories.json"
_MCC_CODES_FILE = Path(__file__).parent.parent / "data" / "mcc_codes.json"

# Flat map: mcc_code -> (top, mid, leaf)  built once at import time
_mcc_index: dict[str, tuple[str, str, str]] = {}


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
        for mid, codes in mids.items():
            for code in codes:
                _mcc_index[code] = (top, mid, descs.get(code, mid))


def categorize(raw_name: str, mcc: str | None, storage) -> dict | None:
    """
    Return a category dict or None if LLM categorization is needed.

    Resolution order:
      1. merchants table cache (any source)
      2. MCC lookup in categories.json
      3. None  →  caller must do LLM categorization and store the result
    """
    _load_index()

    # 1 — merchant cache
    cached = storage.merchant_get(raw_name)
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
            )
            return {
                "category_top":  top,
                "category_mid":  mid,
                "category_leaf": leaf,
                "resolved_name": "",
                "mcc":           mcc,
                "source":        "mcc_lookup",
            }

    # 3 — signal LLM needed
    return None
