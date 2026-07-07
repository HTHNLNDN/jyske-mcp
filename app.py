# This file must never call the Enable Banking API directly for financial
# data (balances/transactions) — that all comes from SQLite, see
# cron/sync.py. The one exception is the consent/re-authorization bootstrap
# (lib/consent.py), which necessarily talks to Enable Banking's /auth and
# /sessions endpoints as part of the OAuth redirect flow.

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import uvicorn
import threading
import time
import json
import os
import logging
from datetime import datetime, timezone
from lib.storage import Storage
from lib.categorizer import category_tree
from lib import consent as consent_lib
from lib.models import all_model_ids, load_catalog
from cron.sync import run_sync
from lib.llm import (
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

load_dotenv()

from server import (
    list_accounts,
    get_balances,
    get_transactions,
    categorize_transaction,
    get_sync_status,
    get_memory,
    update_memory,
    set_budget,
    get_budget_status,
    get_goals,
    set_goal,
    update_goal_progress,
    get_onboarding_status,
    set_onboarding_stage,
    complete_onboarding,
    get_overspend_patterns,
    get_spending,
    compare_spending,
    goal_pace,
    recurring_charges,
    confirm_recurring_status,
    get_current_tip,
    submit_tip_feedback,
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

_sync_lock = threading.Lock()
_sync_state = {"running": False, "error": None, "started_at": None}

_dir = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT = open(os.path.join(_dir, "SYSTEM_PROMPT.md")).read()

# Built Vue frontend (produced by `make build` → frontend/vite.config.js outDir).
DIST_DIR = os.path.join(_dir, "static", "dist")
DIST_INDEX = os.path.join(DIST_DIR, "index.html")
DIST_ASSETS = os.path.join(DIST_DIR, "assets")

def _setup_chat_log() -> logging.Logger:
    log_dir = os.path.expanduser("~/.config/mcp-bank")
    os.makedirs(log_dir, exist_ok=True)
    log = logging.getLogger("mcp_bank.chat")
    if not log.handlers:
        h = logging.FileHandler(os.path.join(log_dir, "chat.log"))
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.DEBUG)
        log.propagate = False
    return log


_chat_log = _setup_chat_log()

TOOLS = [
    {
        "name": "get_memory",
        "description": (
            "Always call this at the start of every session.\n"
            "Returns the user profile (goals, preferences, known patterns)\n"
            "and the last 3 session summaries in a compact format."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_accounts",
        "description": "List all bank accounts from the active consent session.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_balances",
        "description": (
            "Get balances for one or all accounts.\n"
            "Leave account_uid empty to fetch all accounts.\n"
            "Use list_accounts to find account UIDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to fetch balances for. Leave empty for all accounts.",
                }
            },
        },
    },
    {
        "name": "get_transactions",
        "description": (
            "Get transactions for an account.\n"
            "Use list_accounts to find account UIDs.\n"
            "date_from and date_to are optional ISO dates (YYYY-MM-DD); defaults to last 30 days."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to fetch transactions for.",
                },
                "date_from": {
                    "type": "string",
                    "description": "Start date in ISO format YYYY-MM-DD.",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date in ISO format YYYY-MM-DD.",
                },
            },
            "required": ["account_uid"],
        },
    },
    {
        "name": "categorize_transaction",
        "description": (
            "Categorize a merchant by name and optional MCC code.\n\n"
            "Two-step flow:\n"
            "  - Call without llm_category: tries merchant cache then MCC lookup.\n"
            '    Returns the category on hit, or {"needs_llm": true, "raw_name": ...}\n'
            "    to signal that Claude should determine the category and call again.\n"
            '  - Call with llm_category (format "Top > Mid > Leaf"): stores the\n'
            "    LLM-derived category and returns it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_name": {
                    "type": "string",
                    "description": "Raw merchant name from transaction.",
                },
                "mcc": {
                    "type": "string",
                    "description": "Optional merchant category code.",
                },
                "llm_category": {
                    "type": "string",
                    "description": "Optional LLM-derived category in 'Top > Mid > Leaf' format.",
                },
            },
            "required": ["raw_name"],
        },
    },
    {
        "name": "get_sync_status",
        "description": "Returns when data was last synced. Call this as part of every opening brief.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_budget",
        "description": (
            "Set a spending budget for a category.\n"
            "category must be a top-level category name from the taxonomy.\n"
            "period defaults to 'monthly'. Replaces any existing budget for that category+period."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Top-level category name (e.g. 'Food & Dining').",
                },
                "limit_amount": {
                    "type": "number",
                    "description": "Spending limit for the period.",
                },
                "period": {
                    "type": "string",
                    "description": "Budget period — 'monthly' (default) or 'weekly'.",
                },
            },
            "required": ["category", "limit_amount"],
        },
    },
    {
        "name": "get_budget_status",
        "description": "Get current budget status. Always call this as part of the opening brief.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_memory",
        "description": (
            "Always call this at the end of every session.\n"
            "session_summary: 2-3 sentence plain language summary of what happened this session.\n"
            "profile_updates: JSON string of profile keys to update. Valid keys:\n"
            "  - 'preferences': how user likes data presented, language preference, categories they care about\n"
            "  - 'patterns': recurring behaviors or anomalies worth remembering long-term\n"
            "  - 'pending': things flagged but not resolved, awaiting follow-up next session\n"
            "Goals are no longer stored here — use set_goal / update_goal_progress.\n"
            "Only include keys that actually changed this session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_summary": {
                    "type": "string",
                    "description": "2-3 sentence summary of the session.",
                },
                "profile_updates": {
                    "type": "string",
                    "description": "JSON string of profile keys to update.",
                },
            },
            "required": ["session_summary"],
        },
    },
    {
        "name": "get_goals",
        "description": "Get all active goals with progress.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_goal",
        "description": "Create a new savings or spending goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short name for the goal.",
                },
                "target_amount": {
                    "type": "number",
                    "description": "Amount to reach.",
                },
                "purpose": {
                    "type": "string",
                    "description": "What the goal is for.",
                },
                "deadline": {
                    "type": "string",
                    "description": "Target date for the goal (ISO date or free text).",
                },
            },
            "required": ["name", "target_amount", "purpose", "deadline"],
        },
    },
    {
        "name": "update_goal_progress",
        "description": "Update progress on a goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "integer",
                    "description": "ID of the goal to update.",
                },
                "current_amount": {
                    "type": "number",
                    "description": "New current progress amount.",
                },
            },
            "required": ["goal_id", "current_amount"],
        },
    },
    {
        "name": "get_onboarding_status",
        "description": "Check if budget onboarding is complete. Returns current stage if not.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_onboarding_stage",
        "description": (
            "Record progress through budget onboarding. Call once per stage as the user answers.\n"
            "Only pass the fields relevant to the stage just completed; stage moves the\n"
            "onboarding record to the next step ('income' -> 'fixed_costs' -> 'savings' -> 'style' -> 'complete')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Next onboarding stage: 'fixed_costs', 'savings', 'style', or 'complete'.",
                },
                "income": {
                    "type": "number",
                    "description": "Take-home income amount.",
                },
                "income_day": {
                    "type": "integer",
                    "description": "Day of month income usually lands.",
                },
                "fixed_costs": {
                    "type": "string",
                    "description": "Recurring non-negotiable costs, as free text or JSON.",
                },
                "savings_monthly": {
                    "type": "number",
                    "description": "Amount to save per month toward the goal.",
                },
                "savings_purpose": {
                    "type": "string",
                    "description": "What the savings are for.",
                },
                "savings_target": {
                    "type": "number",
                    "description": "Total savings target amount.",
                },
                "savings_deadline": {
                    "type": "string",
                    "description": "Target date for the savings goal.",
                },
                "budget_style": {
                    "type": "string",
                    "description": "How blunt the user wants budget talk to be, e.g. 'honest' or 'gentle'.",
                },
            },
            "required": ["stage"],
        },
    },
    {
        "name": "complete_onboarding",
        "description": "Mark budget onboarding as complete.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_overspend_patterns",
        "description": "Returns categories overspent 3+ consecutive months. Call monthly.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_spending",
        "description": (
            "Sum spending (debits only) between two ISO dates.\n"
            "Defaults date_from/date_to to the current calendar month if left empty.\n"
            "Use this instead of summing a get_transactions listing by hand."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "Start date in ISO format YYYY-MM-DD. Defaults to the 1st of the current month.",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date in ISO format YYYY-MM-DD. Defaults to today.",
                },
                "category": {
                    "type": "string",
                    "description": "Top-level category name to narrow the sum to. Leave empty for all categories.",
                },
                "group_by": {
                    "type": "string",
                    "description": "'category' (default), 'mid', 'month', or 'none'.",
                },
                "account_uid": {
                    "type": "string",
                    "description": "Account UID to narrow the sum to. Leave empty for all accounts.",
                },
            },
        },
    },
    {
        "name": "compare_spending",
        "description": (
            "Compare total spending in one month against a baseline month (both 'YYYY-MM').\n"
            "Defaults month to the current calendar month and baseline_month to the month before it.\n"
            "Use this instead of eyeballing two get_transactions listings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "Month to compare, 'YYYY-MM'. Defaults to the current month.",
                },
                "baseline_month": {
                    "type": "string",
                    "description": "Baseline month, 'YYYY-MM'. Defaults to the month before `month`.",
                },
                "category": {
                    "type": "string",
                    "description": "Top-level category to narrow to (breaks down by mid-category instead).",
                },
            },
        },
    },
    {
        "name": "goal_pace",
        "description": (
            "Compute pacing math for active goals: percent complete, whether on track\n"
            "for the deadline, and the daily/monthly amount required to still hit it.\n"
            "goal_id = 0 (default) means all active goals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {
                    "type": "integer",
                    "description": "ID of a specific goal, or 0 for all active goals.",
                },
            },
        },
    },
    {
        "name": "recurring_charges",
        "description": (
            "Detect recurring/subscription-like charges and frequent merchants from\n"
            "transaction history. Flags merchants that have gone quiet (needs_confirmation)\n"
            "so the agent can ask the user whether they cancelled it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lookback_days": {
                    "type": "integer",
                    "description": "How many days of history to scan. Defaults to 180.",
                },
                "min_count": {
                    "type": "integer",
                    "description": "Minimum charge count to consider a merchant. Defaults to 3.",
                },
            },
        },
    },
    {
        "name": "confirm_recurring_status",
        "description": (
            "Record the user's answer to a cancellation-confirmation question raised by\n"
            "recurring_charges (needs_confirmation: true). status must be 'active', 'cancelled', or 'unknown'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {
                    "type": "string",
                    "description": "Merchant name as shown by recurring_charges.",
                },
                "status": {
                    "type": "string",
                    "description": "'active', 'cancelled', or 'unknown'.",
                },
                "currency": {
                    "type": "string",
                    "description": "Currency of the recurring charge. Defaults to 'DKK'.",
                },
            },
            "required": ["merchant", "status"],
        },
    },
    {
        "name": "get_current_tip",
        "description": (
            "Returns today's financial tip of the day, if one was generated overnight.\n"
            "Call this opportunistically — as part of the opening brief, or whenever\n"
            "the user's message could plausibly be reacting to a tip."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_tip_feedback",
        "description": (
            "Record the user's conversational reaction to a tip. verdict must be\n"
            "'accepted' or 'rejected' — always call this with an explicit verdict when\n"
            "the user pushes back on or endorses a tip. reason_text is required: capture\n"
            "the user's actual words/reasoning, never just the verdict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tip_id": {
                    "type": "integer",
                    "description": "ID of the tip, from get_current_tip.",
                },
                "verdict": {
                    "type": "string",
                    "description": "'accepted' or 'rejected'.",
                },
                "reason_text": {
                    "type": "string",
                    "description": "The user's actual reasoning — required.",
                },
                "reason_code": {
                    "type": "string",
                    "description": (
                        "Optional classifier: not_representative, already_addressed, "
                        "not_actionable, inaccurate, not_relevant, other."
                    ),
                },
            },
            "required": ["tip_id", "verdict", "reason_text"],
        },
    },
]

# LiteLLM (and every non-Anthropic provider it talks to) expects tools in
# OpenAI's function-calling shape, not Anthropic's {name, description,
# input_schema}. Convert once at import time; litellm re-translates this
# into the Anthropic tool format under the hood when LLM_MODEL is a Claude
# model, so a single TOOLS list keeps working across providers.
def _tools_for_litellm(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


LITELLM_TOOLS = _tools_for_litellm(TOOLS)

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


def _run_tool(name: str, inputs: dict) -> str:
    dispatch = {
        "get_memory":             lambda i: get_memory(),
        "list_accounts":          lambda i: list_accounts(),
        "get_balances":           lambda i: get_balances(**i),
        "get_transactions":       lambda i: get_transactions(**i),
        "categorize_transaction": lambda i: categorize_transaction(**i),
        "get_sync_status":        lambda i: get_sync_status(),
        "set_budget":             lambda i: set_budget(**i),
        "get_budget_status":      lambda i: get_budget_status(),
        "update_memory":          lambda i: update_memory(**i),
        "get_goals":              lambda i: get_goals(),
        "set_goal":               lambda i: set_goal(**i),
        "update_goal_progress":   lambda i: update_goal_progress(**i),
        "get_onboarding_status":  lambda i: get_onboarding_status(),
        "set_onboarding_stage":   lambda i: set_onboarding_stage(**i),
        "complete_onboarding":    lambda i: complete_onboarding(),
        "get_overspend_patterns": lambda i: get_overspend_patterns(),
        "get_spending":           lambda i: get_spending(**i),
        "compare_spending":       lambda i: compare_spending(**i),
        "goal_pace":              lambda i: goal_pace(**i),
        "recurring_charges":      lambda i: recurring_charges(**i),
        "confirm_recurring_status": lambda i: confirm_recurring_status(**i),
        "get_current_tip":        lambda i: get_current_tip(),
        "submit_tip_feedback":    lambda i: submit_tip_feedback(**i),
    }
    fn = dispatch.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    try:
        return fn(inputs)
    except Exception as e:
        return f"Tool error ({name}): {e}"


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


class TipFeedbackRequest(BaseModel):
    tip_id: int
    reason_text: str


class SyncTriggerRequest(BaseModel):
    months_back: int | None = None


class RecategorizeRequest(BaseModel):
    transaction_id: int  # maps to transactions.id (the primary key) — NOT
                         # transactions.transaction_id (the bank's own unique
                         # reference column).
    category_top: str
    category_mid: str


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
    storage = Storage()
    agents = storage.get_agents()
    result = []
    for a in agents:
        model = a.get("model")
        configured = bool(model) and bool(storage.get_provider_key(model.split("/")[0]))
        result.append({**a, "configured": configured})
    return result


class ModelUpdateRequest(BaseModel):
    model: str


class ProviderKeyRequest(BaseModel):
    api_key: str


@app.get("/providers")
def list_providers():
    # Never include actual key values here — has_key is a boolean presence
    # check only (Storage().list_providers_with_keys()), the key itself is
    # never read back out over the API.
    storage = Storage()
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
    Storage().set_provider_key(provider, req.api_key)
    return {"ok": True}


@app.delete("/providers/{provider}/key")
def delete_provider_key(provider: str):
    Storage().delete_provider_key(provider)
    return {"ok": True}


@app.put("/agents/{agent_id}/model")
def set_agent_model(agent_id: str, req: ModelUpdateRequest):
    if req.model not in all_model_ids():
        return JSONResponse({"detail": f"Unknown model: {req.model}"}, status_code=400)
    Storage().set_agent_model(agent_id, req.model)
    return {"ok": True}


@app.get("/history")
def get_history():
    # get_all_summaries() returns newest-first; reverse for the inline chat
    # timeline, which reads top-to-bottom oldest-to-newest like a scrollback.
    entries = Storage().get_all_summaries()
    result = []
    for e in entries:
        dt = datetime.fromtimestamp(e["created_at"])
        result.append({
            "date": dt.strftime("%b %d, %Y"),
            "period": dt.strftime("%Y-%m-%d"),
            "summary": e["summary"],
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
        _chat_log.info("USER: %.400s", req.message)

        try:
            llm_cfg = resolve_agent_llm(req.agent_id)
        except LLMNotConfiguredError as e:
            _chat_log.info("NOT CONFIGURED: %s", e)
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
                        tools=LITELLM_TOOLS,
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
                        _chat_log.info("ASSISTANT[%d]: %.800s", iteration, reply_text)

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
                        args_str = json.dumps(args, ensure_ascii=False)
                        _chat_log.info("  TOOL[%d] %s  %s", iteration, c["name"], args_str[:600])
                        tool_span = start_tool_span(trace_id, c["name"], args)
                        result = _run_tool(c["name"], args)
                        end_tool_span(tool_span, result)
                        _chat_log.info("  RESULT: %.600s", str(result))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": c["id"],
                            "content": result,
                        })
        except Exception as e:
            _chat_log.info("ERROR: %s", e)
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
            _chat_log.info("Langfuse feedback score failed: %s", e)
    return {"ok": True}


@app.get("/tip/today")
def tip_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tip = Storage().get_tip_for_date(today)
    if tip is None:
        return {"tip": None}
    return {"tip": tip}


@app.post("/tip/feedback")
def tip_feedback(req: TipFeedbackRequest):
    # UI path specifically — free-text only, no verdict choice (see
    # cron/tips.py / server.submit_tip_feedback for the chat path, which
    # always records an explicit accepted/rejected verdict instead).
    try:
        Storage().set_tip_feedback(
            req.tip_id, "evaluated", None, req.reason_text, source="ui"
        )
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True}


@app.get("/budgets/status")
def budgets_status():
    # Storage().get_budget_status() already returns list[dict] directly —
    # unlike server.get_budget_status() (the MCP tool wrapper, imported
    # above), which json.dumps()s the same rows into a string for the chat
    # tool-call path. Call Storage directly here so the dashboard gets real
    # JSON, not a double-encoded string.
    return {"budgets": Storage().get_budget_status()}


@app.get("/budgets/breakdown")
def budgets_breakdown(category: str):
    # Sub-category drill-down for an expandable budget card: same month
    # window as /budgets/status (via current_month_window()) and the same
    # sum_spending() aggregation path, just grouped by mid instead of top —
    # so `total` here always matches that category's `spent` on the status
    # endpoint.
    storage = Storage()
    date_from, date_to = storage.current_month_window()
    rows = storage.sum_spending(date_from, date_to, category_top=category, group_by="mid")

    by_mid: dict[str | None, dict] = {}
    for row in rows:
        key = row["key"] or None
        entry = by_mid.setdefault(key, {"spent": 0.0, "count": 0})
        entry["spent"] += row["amount"]
        entry["count"] += row["count"]

    breakdown = []
    uncategorized_entry = None
    for key, entry in by_mid.items():
        item = {
            "category_mid": key,
            "label":        key or "Uncategorized",
            "spent":        round(entry["spent"], 2),
            "count":        entry["count"],
            "uncategorized": key is None,
        }
        if key is None:
            uncategorized_entry = item
        else:
            breakdown.append(item)

    breakdown.sort(key=lambda x: x["spent"], reverse=True)
    if uncategorized_entry is not None:
        breakdown.append(uncategorized_entry)

    total = round(sum(item["spent"] for item in breakdown), 2)

    return {
        "category":    category,
        "period_from": date_from,
        "period_to":   date_to,
        "total":       total,
        "breakdown":   breakdown,
    }


@app.get("/budgets/transactions")
def budgets_transactions(
    category: str,
    mid: str | None = None,
    uncategorized: bool = False,
):
    # Line items backing a single row from /budgets/breakdown. Uses the same
    # month window (current_month_window()) and Storage().get_transactions_by_category(),
    # which mirrors sum_spending()'s filters exactly so these always sum to
    # that row's `spent` figure. Never returns raw_data.
    storage = Storage()
    date_from, date_to = storage.current_month_window()
    items = storage.get_transactions_by_category(
        date_from,
        date_to,
        category_top=category,
        category_mid=None if uncategorized else mid,
        uncategorized=uncategorized,
    )
    return {
        "category":     category,
        "category_mid": None if uncategorized else mid,
        "period_from":  date_from,
        "period_to":    date_to,
        "items":        items,
    }


@app.get("/goals")
def goals():
    # Same reasoning as /budgets/status: Storage().get_goals() returns
    # list[dict] directly. goal_pace() is the MCP tool (imported above,
    # reused as-is per convention) and returns a JSON string keyed by
    # "goal_id" per goal — matches Storage().get_goals()'s "id" field.
    base = Storage().get_goals()
    pace = {p["goal_id"]: p for p in json.loads(goal_pace())}
    return {"goals": [{**g, "pace": pace.get(g["id"])} for g in base]}


@app.get("/audit/data")
def audit_data(agent_id: str = "finance"):
    # Data-audit view: everything the agent has on the user, in one
    # unpaginated payload — so the UI can show whether *all* stored data is
    # actually being considered. transactions/profile are account-global (no
    # agent_id column to scope by); summaries are recorded per-agent but
    # currently shown across all agents (see get_all_summaries); budgets/
    # goals/tips are agent-scoped and filtered active-only / by agent_id the
    # same way their existing single-agent endpoints already do above.
    #
    # transactions never includes raw_data — Storage().get_all_transactions()
    # only selects the compact typed columns, never the raw Enable Banking
    # payload (see the no-raw-transaction-data rule this file is bound by).
    storage = Storage()
    transactions = storage.get_all_transactions()
    return {
        "profile":      storage.get_all_profile(),
        "summaries":    storage.get_all_summaries(),
        "budgets":      storage.get_budget_status(agent_id),
        "goals":        storage.get_goals(agent_id),
        "tips":         storage.get_all_tips_with_feedback(agent_id),
        "transactions": {"count": len(transactions), "items": transactions},
    }


@app.get("/consent/status")
def consent_status():
    storage = Storage()
    session = storage.read_session_unchecked()
    last_sync = storage.get_last_sync()

    last_error = None
    if last_sync and last_sync.get("errors"):
        # get_last_sync()'s "errors" holds either a plain error string (session
        # failures, recorded directly by cron/sync.py) or a JSON blob with
        # per-account details (see cron/sync.py's details_payload) — mirror
        # cron/scheduler.py's /sync/status route and pass it through as-is.
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
        result = consent_lib.start_authorization(Storage())
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
        consent_lib.complete_authorization(Storage(), code, state)
        return RedirectResponse("/?consent=success", status_code=302)
    except Exception as e:
        log = logging.getLogger("consent")
        log.error("consent/callback exchange failed: %s", e)
        return RedirectResponse("/?consent=error&reason=exchange_failed", status_code=302)


def _sync_worker(months: int | None) -> None:
    try:
        run_sync(months_back=months)
    except Exception as e:
        _sync_state["error"] = str(e)
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


@app.post("/sync/trigger")
def sync_trigger(req: SyncTriggerRequest):
    # User-facing, authenticated manual sync — distinct from cron/scheduler.py's
    # own :8081 /sync/trigger (internal cron process), which is left untouched.
    months = max(1, min(req.months_back, 12)) if req.months_back is not None else None

    if not _sync_lock.acquire(blocking=False):
        return JSONResponse({"status": "already_running"}, status_code=409)

    # Set state synchronously before returning — avoids a race where an
    # immediate poll of /sync/status could see running: false before the
    # background thread has actually started.
    _sync_state["running"] = True
    _sync_state["error"] = None
    _sync_state["started_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_sync_worker, args=(months,), daemon=True).start()
    return JSONResponse({"status": "started"}, status_code=202)


@app.get("/sync/status")
def sync_status():
    # last_sync shape mirrors cron/scheduler.py's own /sync/status exactly
    # (ISO-formatted started_at/completed_at, "details" holding the raw
    # errors/per-account JSON blob) so both consumers see the same shape.
    last = Storage().get_last_sync()
    last_sync = None
    if last is not None:
        last_sync = {
            "started_at": datetime.fromtimestamp(last["started_at"]).isoformat(),
            "completed_at": datetime.fromtimestamp(last["completed_at"]).isoformat(),
            "accounts_synced": last["accounts_synced"],
            "transactions_fetched": last["transactions_fetched"],
            "new_transactions": last["new_transactions"],
            "details": last["errors"],
        }
    return {
        "running": _sync_state["running"],
        "error": _sync_state["error"],
        "last_sync": last_sync,
    }


@app.get("/budgets/categories")
def budgets_categories():
    # Single source of truth for the category picker (frontend) and for
    # validating /budgets/recategorize requests — both read category_tree().
    return {"tree": category_tree()}


@app.post("/budgets/recategorize")
def budgets_recategorize(req: RecategorizeRequest):
    tree = category_tree()
    if req.category_top not in tree:
        return JSONResponse(
            {"detail": f"Unknown category_top: {req.category_top}"}, status_code=400
        )
    if req.category_mid not in tree[req.category_top]:
        return JSONResponse(
            {"detail": f"Unknown category_mid: {req.category_mid} for {req.category_top}"},
            status_code=400,
        )

    result = Storage().recategorize_from_transaction(
        req.transaction_id, req.category_top, req.category_mid
    )
    if result is None:
        return JSONResponse({"detail": "Transaction not found"}, status_code=404)

    return {
        "ok": True,
        "raw_name": result["raw_name"],
        "old_category_top": result["old_category_top"],
        "new_category_top": req.category_top,
        "new_category_mid": req.category_mid,
        "transactions_updated": result["transactions_updated"],
    }


# Catch-all (declared last so it never shadows the API routes or mounts above).
# Serves the SPA shell for any unmatched GET, enabling client-side routing.
@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    return _frontend_response()


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
