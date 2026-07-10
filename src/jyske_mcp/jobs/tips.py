"""
Nightly "financial tip of the day" generator. Analyzes the past 3 months of
spending data (via the same deterministic aggregation tools server.py exposes
to chat), asks the main configured LLM for exactly ONE specific, actionable
tip grounded in a real number/merchant/category, and persists it — along with
exactly what data justified it — so accumulated tips + feedback become a
genuine evaluation dataset over time.

Registered from jyske_mcp/jobs/scheduler.py at 04:30, after the 03:00 sync and 04:00
evals jobs. Mirrors jyske_mcp/jobs/evals.py's structure (dotenv/path/logging setup,
never-raise entrypoint) — see that module for the reasoning behind each of
those choices.
"""

import json
from datetime import datetime, timezone, timedelta

# load .env before jyske_mcp/kernel/llm.py reads os.environ at import time
from dotenv import load_dotenv
from jyske_mcp.kernel.config import ENV_FILE, SYNC_LOG_FILE, secure_config_files, secure_rotating_handler
load_dotenv(ENV_FILE)

# Reuse the exact same log file/format as jyske_mcp/kernel/sync.py so tip-generation
# lines show up alongside sync's/evals' own summary lines.
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        secure_rotating_handler(SYNC_LOG_FILE, "%(asctime)s  %(levelname)-8s  %(message)s"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tips")

from jyske_mcp.kernel.categorizer import top_categories
from jyske_mcp.kernel.llm import LLMNotConfiguredError, resolve_agent_llm, simple_completion
from jyske_mcp.storage import Storage
from jyske_mcp.slices.finance.dto import TipDTO

# server.py's tool functions are plain Python functions returning JSON
# strings — same import-and-call pattern app.py already uses for _run_tool,
# not an MCP client round-trip. server.py itself never calls Enable Banking
# directly, so this stays within the "server.py reads from SQLite only" rule.
from jyske_mcp.mcp.server import (
    get_spending,
    compare_spending,
    recurring_charges,
    get_budget_status,
    goal_pace,
    get_overspend_patterns,
)

WINDOW_DAYS = 90       # ~3 months, same convention as sync.py's _BASELINE_DAYS
STALE_SYNC_HOURS = 24  # mirrors get_sync_status()'s own staleness threshold
PROMPT_VERSION = "v1"  # bump manually whenever _tip_prompt's wording changes

storage = Storage()


def _sync_is_stale() -> bool:
    """True if there's no recorded sync, the last one is older than
    STALE_SYNC_HOURS, or it recorded errors — any of which means tonight's
    signals bundle would be built on bad/missing data."""
    last = storage.get_last_sync()
    if last is None:
        return True
    completed = datetime.fromtimestamp(last["completed_at"], tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - completed).total_seconds() / 3600
    if age_hours > STALE_SYNC_HOURS:
        return True
    if last.get("errors"):
        try:
            details = json.loads(last["errors"])
            if details.get("errors"):
                return True
        except (json.JSONDecodeError, TypeError):
            # errors was a plain string (session-level failure), not JSON
            return True
    return False


def _as_json_list(s: str) -> list:
    """get_budget_status/get_overspend_patterns return a plain sentence
    string (e.g. 'No budgets set...') instead of JSON when there's nothing
    to report — guard json.loads accordingly rather than letting a bad
    parse take down tip generation for the whole night."""
    try:
        data = json.loads(s)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _assemble_signals(date_from: str, date_to: str) -> dict:
    return {
        "get_spending_by_month":    json.loads(get_spending(date_from=date_from, date_to=date_to, group_by="month")),
        "get_spending_by_category": json.loads(get_spending(date_from=date_from, date_to=date_to, group_by="category")),
        "compare_spending":         json.loads(compare_spending()),
        "recurring_charges":        json.loads(recurring_charges(lookback_days=90)),
        "get_budget_status":        _as_json_list(get_budget_status()),
        "goal_pace":                json.loads(goal_pace()),
        "get_overspend_patterns":   _as_json_list(get_overspend_patterns()),
    }


def _tip_prompt(signals: dict, recent_tips: list[TipDTO], rejected_subjects: set[str]) -> str:
    signals_json = json.dumps(signals, ensure_ascii=False)

    recent_lines = []
    for t in recent_tips:
        recent_lines.append(
            f"- [{t.tip_date}] ({t.feedback_status}) {t.tip_text!r}"
            + (f" — reason: {t.feedback_reason_text!r}" if t.feedback_reason_text else "")
        )
    recent_block = "\n".join(recent_lines) if recent_lines else "(none yet)"

    rejected_block = ", ".join(sorted(rejected_subjects)) if rejected_subjects else "(none)"
    valid_categories = ", ".join(sorted(top_categories()))

    return (
        "You are generating exactly ONE financial tip of the day for a personal "
        "finance app, grounded strictly in the real signals data below. Return "
        "ONLY a JSON object — no markdown fences, no commentary, no extra keys.\n\n"
        f"Signals (the last ~3 months of this user's spending, verbatim from the app's "
        f"own aggregation tools):\n{signals_json}\n\n"
        f"Recent tips already given (most recent first, with feedback if any):\n{recent_block}\n\n"
        f"Subjects that were explicitly rejected in the last 30 days — do NOT raise "
        f"any of these again: {rejected_block}\n\n"
        "Rules:\n"
        "- The tip must cite a concrete number, merchant name, or category from the "
        "signals above. Generic advice ('spend less on eating out', 'build an "
        "emergency fund') is strictly forbidden — every tip must be specific to "
        "this user's actual data.\n"
        "- Do not repeat any recent tip's substance, and do not re-raise any subject "
        "whose most recent feedback was a rejection, or any subject listed as "
        "explicitly rejected above.\n"
        "- subject_key must be exactly one of: a merchant name that appears in the "
        "recurring_charges or spending signals above, OR a category_top value from "
        f"this exact taxonomy: {valid_categories}, OR a goal name from goal_pace's "
        "output. Nothing else.\n\n"
        "Return exactly this JSON shape:\n"
        '{"tip_text": "<one specific, actionable sentence>", '
        '"subject_key": "<merchant name | category_top | goal name>", '
        '"category_top": "<top-level category this tip relates to, or null>", '
        '"based_on": "<one sentence explaining which signal(s) justify this tip>"}'
    )


def _parse_tip_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not all(k in data for k in ("tip_text", "subject_key", "category_top", "based_on")):
        return None
    if not isinstance(data.get("tip_text"), str) or not data["tip_text"].strip():
        return None
    return data


def _valid_subject_key(subject_key: str, signals: dict) -> bool:
    """subject_key must resolve to a merchant name, a real category_top
    value, or a goal name — see _tip_prompt's rule. Checked against the
    actual signals bundle passed to the model, not just the taxonomy, so a
    merchant/goal name has to be real too."""
    if not subject_key:
        return False
    if subject_key in top_categories():
        return True

    merchants = {r.get("merchant") for r in signals.get("recurring_charges", {}).get("recurring", [])}
    if subject_key in merchants:
        return True

    goal_names = {g.get("name") for g in signals.get("goal_pace", [])}
    if subject_key in goal_names:
        return True

    return False


def run_tips() -> None:
    secure_config_files()
    try:
        _run_tips_inner()
    except Exception as e:
        log.error("Tip generation failed with an unexpected error: %s", e)


def _run_tips_inner() -> None:
    today = datetime.now(timezone.utc)
    tip_date = today.strftime("%Y-%m-%d")

    if storage.get_tip_for_date(tip_date) is not None:
        log.info("Tip already exists for %s, skipping", tip_date)
        return

    if _sync_is_stale():
        log.info("Skipping tip generation: last sync stale/failed")
        return

    try:
        llm_cfg = resolve_agent_llm("finance")
    except LLMNotConfiguredError as e:
        log.info("Skipping tip generation: %s", e)
        return

    date_from = (today - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    signals = _assemble_signals(date_from, date_to)
    signals_json = json.dumps(signals, ensure_ascii=False)

    recent_tips = storage.get_recent_tips_with_feedback(10)
    rejected_subjects = storage.get_rejected_subject_keys(30)

    tip_data = None
    for attempt in range(2):  # one retry on a badly-shaped response, per spec
        prompt = _tip_prompt(signals, recent_tips, rejected_subjects)
        try:
            raw = simple_completion(prompt, model=llm_cfg.model, api_key=llm_cfg.api_key)
        except Exception as e:
            log.error("Tip generation LLM call failed (attempt %d): %s", attempt + 1, e)
            continue

        parsed = _parse_tip_response(raw)
        if parsed is None:
            log.error("Tip generation returned unparseable JSON (attempt %d): %.300s", attempt + 1, raw)
            continue

        if not _valid_subject_key(parsed.get("subject_key", ""), signals):
            log.error(
                "Tip generation returned an out-of-domain subject_key %r (attempt %d)",
                parsed.get("subject_key"), attempt + 1,
            )
            continue

        tip_data = parsed
        break

    if tip_data is None:
        log.info("Skipping tip generation for %s: no valid tip after retry", tip_date)
        return

    try:
        tip_id = storage.create_tip(
            tip_date=tip_date,
            window_from=date_from,
            window_to=date_to,
            tip_text=tip_data["tip_text"],
            subject_key=tip_data.get("subject_key") or None,
            category_top=tip_data.get("category_top") or None,
            based_on=tip_data.get("based_on") or None,
            signals_json=signals_json,
            model=llm_cfg.model,
            prompt_version=PROMPT_VERSION,
        )
    except Exception as e:
        # DB-level UNIQUE(agent_id, tip_date) backstop — a concurrent/racing
        # run already inserted a tip for today.
        log.info("Tip insert skipped for %s (likely a race with another run): %s", tip_date, e)
        return

    log.info("Tip generated for %s (id=%d): %.80s", tip_date, tip_id, tip_data["tip_text"])


if __name__ == "__main__":
    run_tips()
