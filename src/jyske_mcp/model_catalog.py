"""
Curated model catalog loader. The catalog itself lives in
jyske_mcp/data/curated_models.json (a data file, not hardcoded Python, so it's
cheap to update as providers ship new models — see
.agent/research/briefs/litellm-model-identifiers-2026-07-05.md for the
sourcing/caveats on the exact id strings).

Read once and cached at module level; nothing here talks to Enable Banking
or any LLM provider.
"""

import json
from pathlib import Path

_CATALOG_FILE = Path(__file__).resolve().parent / "data" / "curated_models.json"

_catalog: dict | None = None


def load_catalog() -> dict:
    """Returns {provider: {"label": ..., "models": [{"id":..., "label":...}]}}."""
    global _catalog
    if _catalog is None:
        _catalog = json.loads(_CATALOG_FILE.read_text())
    return _catalog


def all_model_ids() -> set[str]:
    """Flattened set of every model id across all providers — used to
    validate a model selection at the API boundary."""
    return {
        m["id"]
        for provider in load_catalog().values()
        for m in provider["models"]
    }
