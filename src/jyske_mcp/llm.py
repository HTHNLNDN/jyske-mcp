"""
Single module for all LLM calls. Model/API-key selection is per-agent and
DB-configured (see jyske_mcp/storage.py's agents/provider_keys tables and
resolve_agent_llm() below) rather than a global env var — the one exception
is jyske_mcp/jobs/sync.py's merchant categorization and jyske_mcp/jobs/evals.py's JUDGE_MODEL,
which are out of scope for that and keep passing an explicit bare model
string, relying on ANTHROPIC_API_KEY from the environment exactly as before.

No caller in this codebase should import anthropic (or any other provider
SDK) directly; go through chat_completion / stream_completion / simple_completion.

This module also owns all Langfuse observability glue (see .env.example for
LANGFUSE_* vars). Langfuse is optional and best-effort: every helper below
degrades to a clean no-op when it's disabled or unconfigured, and no
Langfuse call is ever allowed to raise into the caller — tracing must never
break an actual chat response.
"""

import os
import uuid
from contextlib import nullcontext
from typing import Any, Generator, NamedTuple

import litellm
from langfuse import Langfuse

from jyske_mcp.storage import Storage

litellm.drop_params = True  # silently ignore unsupported provider params


class LLMNotConfiguredError(Exception):
    pass


class AgentLLMConfig(NamedTuple):
    model: str
    api_key: str


def resolve_agent_llm(agent_id: str) -> AgentLLMConfig:
    """
    Resolves an agent's configured model + the API key for that model's
    provider, both DB-backed (see jyske_mcp/storage.py). Raises
    LLMNotConfiguredError with a clear, user-facing message at every step
    where configuration is missing — callers (app.py's /chat, jyske_mcp/jobs/tips.py)
    are expected to catch it and degrade gracefully rather than let it
    surface as a raw exception.
    """
    storage = Storage()
    agent = storage.get_agent(agent_id)
    if agent is None:
        raise LLMNotConfiguredError(f"Unknown agent '{agent_id}'.")

    if not agent.get("model"):
        raise LLMNotConfiguredError(
            "No model selected for this agent — open Settings › Model & keys."
        )

    provider = agent["model"].split("/")[0]
    key = storage.get_provider_key(provider)
    if not key:
        raise LLMNotConfiguredError(
            f"No API key set for {provider} — add it in Settings › Model & keys."
        )

    return AgentLLMConfig(model=agent["model"], api_key=key)


# ── Langfuse ──────────────────────────────────────────────────────────────────
# This project's self-hosted Langfuse server is v2, which only understands
# the classic (pre-OpenTelemetry) ingestion API: `lf.trace(...)` /
# `.generation(...)` / `.span(...)` returning stateful client objects with
# their own `.end(...)`, plus top-level `lf.score(...)`. The langfuse package
# is pinned to the 2.x line for this (see requirements.txt) — SDK v3+ speaks
# OTel-only (`POST /api/public/otel/v1/traces`), a route this v2 server
# doesn't have.
#
# Traces are upserted by id (confirmed live against the v2 server): calling
# lf.trace(id=trace_id, ...) again later with the same id — e.g. bare, with
# no name/user_id, as chat_completion/start_tool_span do below — attaches to
# the same trace and does NOT blank out fields set by an earlier call.

_langfuse_client: Langfuse | None = None
_langfuse_client_initialized = False


def get_langfuse() -> Langfuse | None:
    """
    Returns the shared Langfuse client, or None if tracing is disabled or
    unconfigured. Unlike the v4 SDK (a thread-safe per-public-key
    singleton), this classic 2.x line's Langfuse() constructor spins up its
    own background consumer threads on every call with no built-in dedup —
    so this module caches the client itself at process level the first time
    it's built, making repeated calls (once per chat turn/tool call) cheap
    and safe instead of leaking a thread pool per call.
    """
    global _langfuse_client, _langfuse_client_initialized
    if _langfuse_client_initialized:
        return _langfuse_client
    _langfuse_client_initialized = True

    if os.environ.get("LANGFUSE_ENABLED", "false").lower() != "true":
        return None
    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        return None
    try:
        _langfuse_client = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        )
    except Exception:
        _langfuse_client = None
    return _langfuse_client


def new_trace_id() -> str:
    """
    A 32-char lowercase-hex id. The classic SDK accepts any caller-supplied
    string as a trace/observation id (no W3C trace-id shape requirement like
    the OTel-based v4 SDK had), so this remains a fine opaque per-request id
    for the X-Trace-Id response header in app.py whether or not Langfuse is
    actually enabled.
    """
    return uuid.uuid4().hex


def trace_scope(trace_id: str | None, user_id: str | None = None, trace_name: str | None = None):
    """
    Best-effort binds trace-level attributes (name, user_id) to trace_id up
    front, once, at the start of a request. The classic SDK has no OTel-style
    ambient context propagation — every generation/span created later (across
    tool-calling iterations, from chat_completion/start_tool_span) must be
    attached to the same trace explicitly by trace_id instead, which they
    already are. This is kept as a context manager purely so the `with
    trace_scope(...):` call site in app.py doesn't need to change shape; it
    doesn't need to do anything on exit. A clean no-op when Langfuse is
    disabled/unconfigured or trace_id is None.
    """
    lf = get_langfuse()
    if lf is not None and trace_id is not None:
        try:
            lf.trace(id=trace_id, name=trace_name, user_id=user_id)
        except Exception:
            pass
    return nullcontext()


def _usage_dict(usage: Any) -> dict | None:
    """
    Builds the legacy OpenAI-shaped usage dict (promptTokens/completionTokens/
    totalTokens) rather than the newer usage_details field. Verified live
    against the v2 server: it silently drops usage_details (a field added
    server-side in later Langfuse versions than 2.95.11) but does populate
    real token counts/cost from this legacy `usage` shape.
    """
    if usage is None:
        return None
    details = {
        "promptTokens": getattr(usage, "prompt_tokens", None),
        "completionTokens": getattr(usage, "completion_tokens", None),
        "totalTokens": getattr(usage, "total_tokens", None),
    }
    details = {k: v for k, v in details.items() if v is not None}
    return details or None


def end_generation(generation, output: Any = None, usage: Any = None, error: Exception | None = None) -> None:
    """
    Finalize a Langfuse generation handle returned by chat_completion(). This
    is the piece that matters for stream=True: the caller (app.py) accumulates
    the full streamed text/tool-calls itself, then calls this once with the
    real output/usage instead of ending the generation empty. No-ops if
    generation is None (Langfuse disabled/unconfigured) and never raises.
    """
    if generation is None:
        return
    try:
        generation.end(
            output=output,
            usage=_usage_dict(usage),
            level="ERROR" if error else None,
            status_message=str(error) if error else None,
        )
    except Exception:
        pass


def start_tool_span(trace_id: str | None, name: str, tool_input: Any):
    """
    Best-effort Langfuse span for one tool-call. There's no distinct "tool"
    observation type in the classic API (only SPAN/GENERATION/EVENT) — this
    codebase only ever creates spans for tool calls, so a plain span is
    enough to tell them apart from generations when reading traces back
    (see jyske_mcp/jobs/evals.py). Returns None (a safe no-op handle for
    end_tool_span) when there's no trace_id or Langfuse is
    disabled/unconfigured.
    """
    if trace_id is None:
        return None
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        return lf.trace(id=trace_id).span(name=name, input=tool_input)
    except Exception:
        return None


def end_tool_span(span, output: Any = None) -> None:
    if span is None:
        return
    try:
        span.end(output=output)
    except Exception:
        pass


# ── completions ───────────────────────────────────────────────────────────────


def chat_completion(
    messages: list,
    system: str,
    model: str,
    api_key: str,
    stream: bool = False,
    trace_id: str | None = None,
    user_id: str | None = None,
    **kwargs,
):
    """
    model/api_key are the resolved values from resolve_agent_llm() — this
    module no longer reads any model/key from the environment itself.
    trace_id/user_id are optional Langfuse hooks.

    When trace_id is given, this returns (response, generation) instead:
      - non-streaming: the generation is already ended with real output/usage
        by the time this returns.
      - streaming: the generation is started but deliberately left open —
        app.py accumulates the streamed content/tool-calls itself and must
        call end_generation(generation, output=..., usage=...) once the
        stream is fully consumed. Ending it here with output=None would
        record a trace with no output, defeating the point of tracing chat.

    `generation` is None whenever Langfuse is disabled/unconfigured, so
    callers can pass it straight to end_generation()/end_tool_span() either
    way without an extra branch.
    """
    params = {
        "model": model,
        "api_key": api_key,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": stream,
        **kwargs,
    }

    if trace_id is None:
        return litellm.completion(**params)

    lf = get_langfuse()
    generation = None
    if lf is not None:
        try:
            # Bare lf.trace(id=trace_id) attaches to (and upserts) the same
            # trace trace_scope() set name/user_id on earlier in the request
            # — verified live that this doesn't blank those fields out.
            generation = lf.trace(id=trace_id).generation(
                name="chat",
                input=messages,
                model=model,
                metadata={"system_prompt": system},
            )
        except Exception:
            generation = None

    try:
        response = litellm.completion(**params)
    except Exception as e:
        end_generation(generation, error=e)
        raise

    if not stream:
        end_generation(
            generation,
            output=response.choices[0].message.content,
            usage=getattr(response, "usage", None),
        )

    return response, generation


def stream_completion(messages: list, system: str, model: str, api_key: str) -> Generator:
    return chat_completion(messages, system, model, api_key, stream=True)


def simple_completion(prompt: str, model: str, api_key: str | None = None) -> str:
    """
    api_key=None lets LiteLLM fall back to whatever's in the environment —
    this is what jyske_mcp/jobs/sync.py's merchant categorization and jyske_mcp/jobs/evals.py's
    JUDGE_MODEL calls rely on (both explicitly out of scope for per-agent
    DB-configured keys; they keep passing a bare model string and reading
    ANTHROPIC_API_KEY from the environment exactly as before).
    """
    response = litellm.completion(
        model=model,
        api_key=api_key,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
