"""
Nightly LLM-as-judge evaluation of the previous day's /chat traces, scored
back into Langfuse. Registered from cron/scheduler.py at 04:00, after the
03:00 sync job.

This only ever talks to the local .env config and Langfuse's own API for
its own trace/score data — it has nothing to do with Enable Banking, so the
"server.py/cron split" rules elsewhere in this codebase don't apply here.

No-ops cleanly (with a one-line log) when Langfuse is disabled/unconfigured
or there are zero traces to score — never raises.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# project root on path so lib/ imports work regardless of CWD
sys.path.insert(0, str(Path(__file__).parent.parent))

# load .env before lib/llm.py reads os.environ at import time
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

# Reuse the exact same log file/format as cron/sync.py so `Eval complete: ...`
# lines show up alongside sync's own summary lines.
import logging

_LOG_FILE = Path("~/.config/mcp-bank/sync.log").expanduser()
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("evals")

from lib.llm import get_langfuse, simple_completion

# Cheap, fast model for judging — this is a background job, not the
# user-facing chat model (mirrors cron/sync.py's _batch_categorize).
JUDGE_MODEL = "claude-haiku-4-5-20251001"

SCORE_NAMES = ("relevance", "brevity", "tool_precision", "on_topic")


def _judge_prompt(user_message: str, tool_calls: list[dict], final_response: str) -> str:
    tool_calls_json = json.dumps(tool_calls, ensure_ascii=False)
    return (
        "You are grading a personal finance assistant's response to a user message. "
        "Score strictly. Return ONLY a JSON object — no markdown fences, no commentary, "
        "no extra keys.\n\n"
        f"User message:\n{user_message}\n\n"
        f"Tool calls made (name + params):\n{tool_calls_json}\n\n"
        f"Final assistant response:\n{final_response}\n\n"
        "Return exactly this JSON shape:\n"
        '{"relevance": <1-5>, "brevity": <1-5>, "tool_precision": <1-5>, "on_topic": <0 or 1>}\n\n'
        "relevance (1-5): did the response actually answer what the user asked?\n"
        "brevity (1-5): was it appropriately concise, not padded or repetitive?\n"
        "tool_precision (1-5): did it call only the tools actually needed, with no "
        "redundant or irrelevant calls?\n"
        "on_topic (0 or 1): 1 if it stayed within personal finance scope, 0 if it "
        "wandered off-topic."
    )


def _extract_conversation(trace) -> tuple[str, list[dict], str]:
    """
    Pull (user_message, tool_calls, final_response) out of a Langfuse
    TraceWithFullDetails.

    trace.input/trace.output are always None here — confirmed live.
    chat_completion() (lib/llm.py) only ever sets input/output on each
    per-iteration 'chat' GENERATION observation, never on the trace object
    itself, and that was already true before the SDK downgrade; it just
    never surfaced before because trace ingestion silently 404'd against
    this v2 server. So instead: the first GENERATION observation's input
    holds the original user message list, and the last GENERATION
    observation's output holds the final assistant reply (a request may
    run several tool-calling iterations, each its own GENERATION).
    trace.observations includes one SPAN per tool call (see start_tool_span
    in lib/llm.py; classic Langfuse 2.x has no distinct 'tool' observation
    type like the OTel-based v4 SDK had, so a plain SPAN is the marker —
    this codebase only ever creates spans for tool calls).
    Falls back to empty values on unexpected shapes — a judge run on partial
    data is safer than crashing the whole nightly job over one bad trace.
    """
    generations = sorted(
        (o for o in trace.observations or [] if str(getattr(o, "type", "")).upper() == "GENERATION"),
        key=lambda o: o.start_time,
    )

    user_message = ""
    if generations and isinstance(generations[0].input, list):
        for m in generations[0].input:
            if isinstance(m, dict) and m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, str):
                    user_message = content

    final_response = ""
    if generations:
        last_output = generations[-1].output
        final_response = last_output if isinstance(last_output, str) else (last_output or "")

    tool_calls = []
    for obs in trace.observations or []:
        if str(getattr(obs, "type", "")).upper() == "SPAN":
            tool_calls.append({"name": obs.name, "input": obs.input})

    return user_message, tool_calls, str(final_response)


def _judge_trace(trace) -> dict | None:
    user_message, tool_calls, final_response = _extract_conversation(trace)
    if not user_message and not final_response:
        return None

    prompt = _judge_prompt(user_message, tool_calls, final_response)
    try:
        text = simple_completion(prompt, model=JUDGE_MODEL).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        scores = json.loads(text)
    except Exception as e:
        log.error("Judge call failed for trace %s: %s", trace.id, e)
        return None

    if not all(k in scores for k in SCORE_NAMES):
        log.error("Judge response missing keys for trace %s: %s", trace.id, scores)
        return None
    return scores


def run_evals() -> None:
    lf = get_langfuse()
    if lf is None:
        log.info("Eval complete: Langfuse disabled/unconfigured, skipping")
        return

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    try:
        traces_page = lf.api.trace.list(from_timestamp=since, to_timestamp=now, limit=100)
        trace_summaries = list(traces_page.data)
    except Exception as e:
        log.error("Eval complete: failed to list traces from Langfuse — %s", e)
        return

    if not trace_summaries:
        log.info("Eval complete: 0 traces scored (no traces in the last 24h)")
        return

    scored = 0
    relevance_total = 0.0

    for summary in trace_summaries:
        try:
            # 2.60.x's TraceClient.get() has no `fields` param (that was a
            # v4-only fern client addition) — TraceWithFullDetails always
            # includes input/output/observations anyway, so just drop it.
            trace = lf.api.trace.get(summary.id)
        except Exception as e:
            log.error("Failed to fetch trace %s: %s", summary.id, e)
            continue

        scores = _judge_trace(trace)
        if scores is None:
            continue

        try:
            for name in SCORE_NAMES:
                lf.score(trace_id=trace.id, name=name, value=scores[name])
        except Exception as e:
            log.error("Failed to post scores for trace %s: %s", trace.id, e)
            continue

        scored += 1
        relevance_total += scores["relevance"]

    try:
        lf.flush()
    except Exception:
        pass

    avg_relevance = (relevance_total / scored) if scored else 0.0
    log.info("Eval complete: %d traces scored, avg relevance %.1f", scored, avg_relevance)


if __name__ == "__main__":
    run_evals()
