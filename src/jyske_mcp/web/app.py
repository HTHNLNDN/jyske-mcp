# This file must never call the Enable Banking API directly for financial
# data (balances/transactions) — that all comes from SQLite, see
# jyske_mcp/kernel/sync.py. The one exception is the consent/re-authorization bootstrap
# (jyske_mcp/kernel/consent.py), which necessarily talks to Enable Banking's /auth and
# /sessions endpoints as part of the OAuth redirect flow.

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import requests
import uvicorn
import time
import json
import os
import logging
from datetime import datetime, timezone
from jyske_mcp.kernel.config import ENV_FILE, ROOT_DIR, CHAT_LOG_FILE, secure_config_files, secure_rotating_handler
from jyske_mcp.kernel.storage import KernelStorage
from jyske_mcp.kernel import consent as consent_lib
from jyske_mcp.kernel import scheduler_client
from jyske_mcp.kernel.model_catalog import all_model_ids, load_catalog
from jyske_mcp.kernel.llm import (
    chat_completion,
    resolve_agent_llm,
    LLMNotConfiguredError,
    get_langfuse,
    new_trace_id,
    trace_scope,
    start_tool_span,
    end_tool_span,
    end_generation,
)

load_dotenv(ENV_FILE)

# The chat loop's system prompt + tool schema/dispatch, the finance HTTP
# routes (mounted below), and the finance portion of /audit/data all come
# from the finance slice's public api.py — platform never reaches into
# slice internals for these (see .agent/epics/vsa-restructure-blueprint.md
# §4/§6). TOOL_REGISTRY is the single ToolSpec-derived source for both the
# LiteLLM tool schema and the name->callable dispatch (epic deliverable #8).
from jyske_mcp.slices.finance.api import (
    PROMPT as SYSTEM_PROMPT,
    TOOL_REGISTRY,
    router as finance_router,
    audit_section as finance_audit_section,
)

APP_PIN = os.environ["APP_PIN"]
SESSION_SECRET = os.environ["SESSION_SECRET"]

serializer = URLSafeTimedSerializer(SESSION_SECRET)
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 86400  # 24 hours

_failed_attempts = 0
_lockout_until: float = 0.0
MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 60

_dir = str(ROOT_DIR)  # repo root — static/ and the frontend build live here, not in the package

# Built Vue frontend (produced by `make build` → frontend/vite.config.js outDir).
DIST_DIR = os.path.join(_dir, "static", "dist")
DIST_INDEX = os.path.join(DIST_DIR, "index.html")
DIST_ASSETS = os.path.join(DIST_DIR, "assets")

def _setup_chat_log() -> logging.Logger:
    log = logging.getLogger("mcp_bank.chat")
    if not log.handlers:
        h = secure_rotating_handler(
            CHAT_LOG_FILE, "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        log.addHandler(h)
        log.setLevel(logging.DEBUG)
        log.propagate = False
    return log


# Idempotent — chmods cache.db/session.json/chat.log/sync.log/.env to 0600 on
# every process start, not just on first creation (see config.secure_config_files).
secure_config_files()
_chat_log = _setup_chat_log()

# Default-deny: every request needs a valid session cookie unless its path is
# exempt below. Any new route is therefore protected automatically.
EXEMPT_EXACT = {
    "/auth/login", "/auth/logout", "/sw.js", "/manifest.json", "/favicon.ico",
    "/consent/callback",
}
EXEMPT_PREFIXES = ("/static/", "/assets/")


def _is_exempt(request: Request) -> bool:
    path = request.url.path
    if request.method == "GET" and path == "/":
        return True
    if path in EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not _is_exempt(request):
            token = request.cookies.get(SESSION_COOKIE)
            if not token:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            try:
                serializer.loads(token, max_age=SESSION_MAX_AGE)
            except (BadSignature, SignatureExpired):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


app = FastAPI()
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=os.path.join(_dir, "static")), name="static")

# Vite emits hashed JS/CSS under static/dist/assets. Only mount it when a build
# exists so the dev workflow (API via `make start`, frontend via `make dev`)
# can run app.py without a prior build.
if os.path.isdir(DIST_ASSETS):
    app.mount("/assets", StaticFiles(directory=DIST_ASSETS), name="assets")

# The finance slice's HTTP routes (/budgets/*, /goals, /tip/*), mounted here
# rather than defined in this file — see slices/finance/routes.py and
# .agent/epics/vsa-restructure-blueprint.md §6. AuthMiddleware above is
# added to the app, not per-router, so it wraps these exactly as it wraps
# every other route declared directly on `app`.
app.include_router(finance_router)


class LoginRequest(BaseModel):
    pin: str


class ChatRequest(BaseModel):
    message: str
    agent_id: str
    history: list = []


class FeedbackRequest(BaseModel):
    trace_id: str
    score: int
    comment: str | None = None


class SyncTriggerRequest(BaseModel):
    months_back: int | None = None


def _frontend_response():
    if os.path.exists(DIST_INDEX):
        return FileResponse(DIST_INDEX)
    return JSONResponse(
        {"detail": "Frontend not built. Run `make build`."},
        status_code=503,
    )


@app.get("/sw.js")
def service_worker():
    sw_path = os.path.join(DIST_DIR, "sw.js")
    if not os.path.exists(sw_path):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/")
def home():
    return _frontend_response()


@app.post("/auth/login")
def login(req: LoginRequest, response: Response):
    global _failed_attempts, _lockout_until

    now = time.time()
    if _lockout_until > now:
        remaining = int(_lockout_until - now)
        return JSONResponse(
            {"detail": f"Too many failed attempts. Try again in {remaining}s."},
            status_code=429,
        )

    if req.pin == APP_PIN:
        _failed_attempts = 0
        token = serializer.dumps("authenticated")
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_MAX_AGE,
        )
        return {"status": "ok"}

    _failed_attempts += 1
    if _failed_attempts >= MAX_ATTEMPTS:
        _lockout_until = now + LOCKOUT_SECONDS
        _failed_attempts = 0
        return JSONResponse(
            {"detail": "Too many failed attempts. Locked out for 60s."},
            status_code=429,
        )

    remaining = MAX_ATTEMPTS - _failed_attempts
    return JSONResponse(
        {"detail": f"Invalid PIN. {remaining} attempt(s) remaining."},
        status_code=401,
    )


@app.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/agents")
def list_agents():
    storage = KernelStorage()
    agents = storage.get_agents()
    result = []
    for a in agents:
        model = a.model
        configured = bool(model) and bool(storage.get_provider_key(model.split("/")[0]))
        result.append({**a.model_dump(), "configured": configured})
    return result


class ModelUpdateRequest(BaseModel):
    model: str


class ProviderKeyRequest(BaseModel):
    api_key: str


@app.get("/providers")
def list_providers():
    # Never include actual key values here — has_key is a boolean presence
    # check only (KernelStorage().list_providers_with_keys()), the key itself
    # is never read back out over the API.
    storage = KernelStorage()
    configured = storage.list_providers_with_keys()
    catalog = load_catalog()
    return [
        {
            "provider": provider,
            "label": info["label"],
            "has_key": provider in configured,
            "models": info["models"],
        }
        for provider, info in catalog.items()
    ]


@app.put("/providers/{provider}/key")
def set_provider_key(provider: str, req: ProviderKeyRequest):
    if provider not in load_catalog():
        return JSONResponse({"detail": f"Unknown provider: {provider}"}, status_code=400)
    KernelStorage().set_provider_key(provider, req.api_key)
    return {"ok": True}


@app.delete("/providers/{provider}/key")
def delete_provider_key(provider: str):
    KernelStorage().delete_provider_key(provider)
    return {"ok": True}


@app.put("/agents/{agent_id}/model")
def set_agent_model(agent_id: str, req: ModelUpdateRequest):
    if req.model not in all_model_ids():
        return JSONResponse({"detail": f"Unknown model: {req.model}"}, status_code=400)
    KernelStorage().set_agent_model(agent_id, req.model)
    return {"ok": True}


@app.get("/history")
def get_history():
    # get_all_summaries() returns newest-first; reverse for the inline chat
    # timeline, which reads top-to-bottom oldest-to-newest like a scrollback.
    entries = KernelStorage().get_all_summaries()
    result = []
    for e in entries:
        dt = datetime.fromtimestamp(e.created_at)
        result.append({
            "date": dt.strftime("%b %d, %Y"),
            "period": dt.strftime("%Y-%m-%d"),
            "summary": e.summary,
        })
    result.reverse()
    return result


@app.post("/chat")
def chat(req: ChatRequest):
    # Per-request Langfuse trace id. Generated unconditionally (cheap) so the
    # frontend always gets an X-Trace-Id to attach /feedback to, whether or
    # not Langfuse is actually enabled/configured.
    trace_id = new_trace_id()
    chat_user_id = "claes"

    def generate():
        _chat_log.info("─── CHAT START ───────────────────────────────────────────")
        _chat_log.info("USER: <%d chars>", len(req.message))

        try:
            llm_cfg = resolve_agent_llm(req.agent_id)
        except LLMNotConfiguredError as e:
            _chat_log.info("NOT CONFIGURED: %s", type(e).__name__)
            yield f"data: [ERROR:NOT_CONFIGURED] {e}\n\n"
            return

        try:
            messages = list(req.history) + [{"role": "user", "content": req.message}]

            # Anthropic prompt caching is an Anthropic-specific optimization;
            # only ask for it when the configured model is actually a Claude
            # model. litellm.drop_params silently ignores it otherwise.
            # (Catalog ids are provider-prefixed, e.g. "anthropic/claude-sonnet-5"
            # — check for "claude" anywhere in the id rather than a strict
            # startswith, so this still gates correctly.)
            extra_params = {}
            if "claude" in llm_cfg.model:
                extra_params["cache_control"] = {"type": "ephemeral"}
            if get_langfuse() is not None:
                # Ask for a trailing usage-only chunk so end_generation() can
                # record real token counts instead of ending the trace blank.
                # litellm.drop_params silently ignores this on providers that
                # don't support it (see module docstring).
                extra_params["stream_options"] = {"include_usage": True}

            with trace_scope(trace_id, user_id=chat_user_id, trace_name="chat"):
                for iteration in range(1, 11):
                    response, generation = chat_completion(
                        messages,
                        SYSTEM_PROMPT,
                        model=llm_cfg.model,
                        api_key=llm_cfg.api_key,
                        stream=True,
                        tools=TOOL_REGISTRY.litellm_schemas(),
                        max_tokens=8096,
                        trace_id=trace_id,
                        user_id=chat_user_id,
                        **extra_params,
                    )

                    reply_chunks: list[str] = []
                    tool_calls: dict[int, dict] = {}
                    finish_reason = None
                    usage = None

                    for chunk in response:
                        if getattr(chunk, "usage", None):
                            usage = chunk.usage
                        if not chunk.choices:
                            # trailing usage-only chunk (stream_options above)
                            continue
                        choice = chunk.choices[0]
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                        delta = choice.delta
                        if delta.content:
                            reply_chunks.append(delta.content)
                            yield f"data: {json.dumps(delta.content)}\n\n"
                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                entry = tool_calls.setdefault(
                                    tc.index, {"id": None, "name": "", "arguments": ""}
                                )
                                if tc.id:
                                    entry["id"] = tc.id
                                if tc.function:
                                    if tc.function.name:
                                        entry["name"] += tc.function.name
                                    if tc.function.arguments:
                                        entry["arguments"] += tc.function.arguments

                    reply_text = "".join(reply_chunks)
                    if reply_text:
                        _chat_log.info("ASSISTANT[%d]: <%d chars>", iteration, len(reply_text))

                    end_generation(
                        generation,
                        output=reply_text or {
                            "tool_calls": [
                                {"name": c["name"], "arguments": c["arguments"]}
                                for c in tool_calls.values()
                            ]
                        },
                        usage=usage,
                    )

                    if finish_reason != "tool_calls" or not tool_calls:
                        _chat_log.info(
                            "─── CHAT END (%d iteration(s), stop=%s) ─────────────────",
                            iteration, finish_reason,
                        )
                        break

                    ordered_calls = [tool_calls[i] for i in sorted(tool_calls)]
                    messages.append({
                        "role": "assistant",
                        "content": reply_text or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for c in ordered_calls
                        ],
                    })

                    for c in ordered_calls:
                        try:
                            args = json.loads(c["arguments"]) if c["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        # Tool name only — never log arguments or results,
                        # which can carry balances/IBANs/merchant detail.
                        _chat_log.info("  TOOL[%d] %s", iteration, c["name"])
                        tool_span = start_tool_span(trace_id, c["name"], args)
                        result = TOOL_REGISTRY.dispatch(c["name"], args)
                        end_tool_span(tool_span, result)
                        _chat_log.info("  RESULT: <%d chars>", len(str(result)))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": c["id"],
                            "content": result,
                        })
        except Exception as e:
            _chat_log.info("ERROR: %s", type(e).__name__)
            yield f"data: [ERROR] {e}\n\n"
            return
        yield "data: [DONE]\n\n"

    resp = StreamingResponse(generate(), media_type="text/event-stream")
    resp.headers["X-Trace-Id"] = trace_id
    return resp


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    # Same auth gate as /chat: not in AuthMiddleware's exempt list above, so
    # the default-deny session-cookie check already protects this route.
    lf = get_langfuse()
    if lf is not None:
        try:
            lf.score(
                trace_id=req.trace_id,
                name="user_feedback",
                value=req.score,
                data_type="NUMERIC",
                comment=req.comment,
            )
        except Exception as e:
            _chat_log.info("Langfuse feedback score failed: %s", type(e).__name__)
    return {"ok": True}


@app.get("/audit/data")
def audit_data(agent_id: str = "finance"):
    # Data-audit view: everything the agent has on the user, in one
    # unpaginated payload — so the UI can show whether *all* stored data is
    # actually being considered. transactions/profile are account-global (no
    # agent_id column to scope by); summaries are recorded per-agent but
    # currently shown across all agents (see get_all_summaries); budgets/
    # goals/tips are agent-scoped and filtered active-only / by agent_id —
    # that finance-domain assembly now lives in slices/finance/api.py's
    # audit_section(), which this route merges in below.
    #
    # transactions never includes raw_data — KernelStorage().get_all_transactions()
    # only selects the compact typed columns, never the raw Enable Banking
    # payload (see the no-raw-transaction-data rule this file is bound by).
    storage = KernelStorage()
    transactions = storage.get_all_transactions()
    return {
        "profile":      storage.get_all_profile(),
        "summaries":    [s.model_dump() for s in storage.get_all_summaries()],
        "transactions": {"count": len(transactions), "items": [t.model_dump() for t in transactions]},
        **finance_audit_section(agent_id),
    }


@app.get("/consent/status")
def consent_status():
    storage = KernelStorage()
    session = storage.read_session_unchecked()
    last_sync = storage.get_last_sync()

    last_error = None
    if last_sync and last_sync.get("errors"):
        # get_last_sync()'s "errors" holds either a plain error string (session
        # failures, recorded directly by jyske_mcp/kernel/sync.py) or a JSON blob with
        # per-account details (see jyske_mcp/kernel/sync.py's details_payload) — mirror
        # jyske_mcp/jobs/scheduler.py's /sync/status route and pass it through as-is.
        last_error = last_sync["errors"]

    if session is None:
        return {
            "status": "none",
            "session_id": None,
            "valid_until": None,
            "days_remaining": None,
            "accounts": [],
            "last_error": last_error,
        }

    valid_until_raw = session.get("valid_until")
    valid_until = datetime.fromisoformat(valid_until_raw) if valid_until_raw else None
    now = datetime.now(timezone.utc)
    days_remaining = (valid_until - now).days if valid_until else None

    session_expired = valid_until is not None and valid_until < now
    sync_says_expired = bool(last_error) and "EXPIRED_SESSION" in last_error

    if session_expired or sync_says_expired:
        status = "expired"
    elif days_remaining is not None and days_remaining <= consent_lib.WARN_DAYS:
        status = "expiring_soon"
    else:
        status = "connected"

    accounts = [
        {
            "uid": acc.get("uid"),
            "product": acc.get("product"),
            "iban": acc.get("account_id", {}).get("iban"),
        }
        for acc in session.get("accounts", [])
    ]

    return {
        "status": status,
        "session_id": session.get("session_id"),
        "valid_until": valid_until_raw,
        "days_remaining": days_remaining,
        "accounts": accounts,
        "last_error": last_error,
    }


@app.post("/consent/start")
def consent_start():
    try:
        result = consent_lib.start_authorization(KernelStorage())
        return {"auth_url": result["auth_url"]}
    except Exception as e:
        return JSONResponse(
            {"detail": f"Failed to start Enable Banking authorization: {e}"},
            status_code=502,
        )


@app.get("/consent/callback")
def consent_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/?consent=error&reason={error}", status_code=302)
    try:
        consent_lib.complete_authorization(KernelStorage(), code, state)
        return RedirectResponse("/?consent=success", status_code=302)
    except Exception as e:
        log = logging.getLogger("consent")
        log.error("consent/callback exchange failed: %s", e)
        return RedirectResponse("/?consent=error&reason=exchange_failed", status_code=302)


@app.post("/sync/trigger")
def sync_trigger(req: SyncTriggerRequest):
    # jyske_mcp/jobs/scheduler.py (internal :8081 process) is the single owner of
    # sync execution — this route just proxies the user-facing, authenticated
    # manual-sync request there via scheduler_client, passing its response
    # (status/status_code) straight through so the frontend sees the exact
    # same shape as before this change.
    try:
        resp = scheduler_client.trigger_sync(req.months_back)
    except requests.RequestException as e:
        log = logging.getLogger("app")
        log.warning("sync trigger proxy failed: %s", e)
        return JSONResponse({"detail": "scheduler unreachable"}, status_code=502)
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.get("/sync/status")
def sync_status():
    # Proxies jyske_mcp/jobs/scheduler.py's /sync/status — that process now owns
    # running/error state as well as last_sync, since it's the only process
    # that actually executes syncs.
    try:
        resp = scheduler_client.get_status()
    except requests.RequestException as e:
        log = logging.getLogger("app")
        log.warning("sync status proxy failed: %s", e)
        return JSONResponse(
            {"running": False, "error": "scheduler unreachable", "last_sync": None},
            status_code=200,
        )
    return JSONResponse(resp.json(), status_code=resp.status_code)


# Catch-all (declared last so it never shadows the API routes or mounts above).
# Serves the SPA shell for any unmatched GET, enabling client-side routing.
@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    return _frontend_response()


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
