# This file must never call the Enable Banking API directly.
# All data comes from SQLite. See cron/sync.py for data fetching.

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import anthropic
import uvicorn
import time
import json
import os
from datetime import datetime
from lib.storage import Storage

load_dotenv()

from server import (
    list_accounts,
    get_balances,
    get_transactions,
    categorize_transaction,
    get_sync_status,
    get_memory,
    update_memory,
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

_dir = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT = open(os.path.join(_dir, "SYSTEM_PROMPT.md")).read()

anthropic_client = anthropic.Anthropic()

AGENTS = [
    {
        "id": "finance",
        "name": "Finance Agent",
        "description": "Personal finance companion",
    }
]

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
        "name": "update_memory",
        "description": (
            "Always call this at the end of every session.\n"
            "session_summary: 2-3 sentence plain language summary of what happened this session.\n"
            "profile_updates: JSON string of profile keys to update. Valid keys:\n"
            "  - 'goals': list of active goals with target, deadline, current progress\n"
            "  - 'preferences': how user likes data presented, language preference, categories they care about\n"
            "  - 'patterns': recurring behaviors or anomalies worth remembering long-term\n"
            "  - 'pending': things flagged but not resolved, awaiting follow-up next session\n"
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
]

PROTECTED_PATHS = {"/chat", "/agents", "/history"}


def _run_tool(name: str, inputs: dict) -> str:
    dispatch = {
        "get_memory":             lambda i: get_memory(),
        "list_accounts":          lambda i: list_accounts(),
        "get_balances":           lambda i: get_balances(**i),
        "get_transactions":       lambda i: get_transactions(**i),
        "categorize_transaction": lambda i: categorize_transaction(**i),
        "get_sync_status":        lambda i: get_sync_status(),
        "update_memory":          lambda i: update_memory(**i),
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
        if request.url.path in PROTECTED_PATHS:
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


class LoginRequest(BaseModel):
    pin: str


class ChatRequest(BaseModel):
    message: str
    agent_id: str
    history: list = []


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        os.path.join(_dir, "static", "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/")
def home():
    return FileResponse(os.path.join(_dir, "templates", "index.html"))


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
    return AGENTS


@app.get("/history")
def get_history():
    entries = Storage().get_all_summaries()
    result = []
    for e in entries:
        dt = datetime.fromtimestamp(e["created_at"])
        result.append({
            "date": dt.strftime("%b %d, %Y"),
            "summary": e["summary"],
        })
    return result


@app.post("/chat")
def chat(req: ChatRequest):
    def generate():
        try:
            messages = list(req.history) + [{"role": "user", "content": req.message}]
            for _ in range(10):
                with anthropic_client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=8096,
                    system=[{
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=TOOLS,
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        yield f"data: {json.dumps(text)}\n\n"
                    final = stream.get_final_message()

                if final.stop_reason != "tool_use":
                    break

                messages.append({"role": "assistant", "content": final.content})
                tool_results = []
                for block in final.content:
                    if block.type == "tool_use":
                        result = _run_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                messages.append({"role": "user", "content": tool_results})
        except Exception as e:
            yield f"data: [ERROR] {e}\n\n"
            return
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
