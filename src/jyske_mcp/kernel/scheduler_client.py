"""Thin HTTP client for talking to jyske_mcp/jobs/scheduler.py's internal
:8081 process — the single owner of sync execution.

No retry/session abstraction on purpose: callers (app.py's /sync/* proxy
routes, consent.py's post-authorization kick) each handle failures/timeouts
in their own way, so this module just makes the two requests calls
consistent and keeps the base URL / auth header in one place.
"""
import os

import requests

SCHEDULER_BASE_URL = os.environ.get("SCHEDULER_URL", "http://localhost:8081")


def _headers() -> dict:
    return {"X-Scheduler-Secret": os.environ.get("SCHEDULER_SECRET", "")}


def trigger_sync(months_back: int | None, timeout: float = 5.0) -> requests.Response:
    return requests.post(
        f"{SCHEDULER_BASE_URL}/sync/trigger",
        json={"months_back": months_back},
        headers=_headers(),
        timeout=timeout,
    )


def get_status(timeout: float = 5.0) -> requests.Response:
    return requests.get(
        f"{SCHEDULER_BASE_URL}/sync/status",
        headers=_headers(),
        timeout=timeout,
    )
