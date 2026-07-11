import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

# load .env before jyske_mcp.kernel.auth (imported transitively below) reads
# os.environ at import time
from dotenv import load_dotenv
from jyske_mcp.kernel.config import ENV_FILE, secure_config_files
load_dotenv(ENV_FILE)

# Idempotent — chmods cache.db/session.json/chat.log/sync.log/.env to 0600 on
# every process start, not just on first creation (see config.secure_config_files).
secure_config_files()

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from jyske_mcp.kernel.sync import run_sync
from jyske_mcp.jobs.evals import run_evals
from jyske_mcp.jobs.tips import run_tips
from jyske_mcp.slices.finance.api import snapshot_budget_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("scheduler")

scheduler = BackgroundScheduler()

# TODO: retired autonomous backlog-refinement job (shelled out to the claude
# CLI from this banking process). If revived it belongs in a separate
# user-level process, never co-located with live-banking sync/evals/tips.

# This process is the single owner of sync execution — app.py's /sync/trigger
# and /sync/status now proxy here (via jyske_mcp/kernel/scheduler_client.py) instead
# of running their own thread/lock, so a manual trigger and the 03:00 cron
# job can never run concurrently.
_sync_lock = threading.Lock()
_sync_state = {"running": False, "error": None, "started_at": None}


def _sync_worker(months_back: int | None) -> None:
    try:
        run_sync(months_back=months_back)
        # Finance-domain post-sync hook, run in the same job/thread as
        # run_sync — never a second sync-execution path (this process
        # remains sync's single owner). See
        # jyske_mcp.slices.finance.api.snapshot_budget_history's docstring.
        snapshot_budget_history()
    except Exception as e:
        _sync_state["error"] = str(e)
    finally:
        _sync_state["running"] = False
        _sync_lock.release()


def _start_sync(months_back: int | None = None) -> bool:
    if not _sync_lock.acquire(blocking=False):
        return False
    _sync_state["running"] = True
    _sync_state["error"] = None
    _sync_state["started_at"] = datetime.now(timezone.utc).isoformat()
    threading.Thread(target=_sync_worker, args=(months_back,), daemon=True).start()
    return True


def check_sync_freshness() -> None:
    from jyske_mcp.slices.finance.storage import Storage
    from jyske_mcp.kernel.sync import is_sync_stale, STALE_SYNC_HOURS
    last = Storage().get_last_sync()
    if is_sync_stale(last, time.time()):
        if last is None:
            log.error("Sync freshness check: no sync has ever completed.")
        else:
            hrs = (time.time() - last["completed_at"]) / 3600
            log.error(
                "Sync freshness check: last sync completed %.1fh ago (threshold %dh) — sync may be wedged.",
                hrs, STALE_SYNC_HOURS,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("SCHEDULER_SECRET"):
        log.warning(
            "SCHEDULER_SECRET is unset/empty — the scheduler will reject all "
            "/sync and /tips trigger requests until it's set."
        )
    scheduler.add_job(_start_sync, "cron", hour=3, minute=0, id="daily_sync")
    scheduler.add_job(run_evals, "cron", hour=4, minute=0, id="nightly_evals")
    scheduler.add_job(run_tips, "cron", hour=4, minute=30, id="nightly_tip")
    scheduler.start()
    log.info(
        "Scheduler started — daily sync at 03:00, nightly evals at 04:00, "
        "nightly tip of the day at 04:30"
    )
    scheduler.add_job(check_sync_freshness, "interval", hours=6, id="sync_freshness")
    check_sync_freshness()
    yield
    scheduler.shutdown()
    log.info("Scheduler stopped")


def require_scheduler_secret(x_scheduler_secret: str | None = Header(default=None)):
    expected = os.environ.get("SCHEDULER_SECRET", "")
    if not expected:
        log.warning("SCHEDULER_SECRET is unset/empty — rejecting scheduler request (fail-closed)")
        raise HTTPException(status_code=503, detail="scheduler auth not configured")
    if not secrets.compare_digest(x_scheduler_secret or "", expected):
        raise HTTPException(status_code=401, detail="invalid or missing scheduler secret")


app = FastAPI(lifespan=lifespan)


class SyncTriggerRequest(BaseModel):
    months_back: int | None = None


@app.post("/sync/trigger", dependencies=[Depends(require_scheduler_secret)])
def trigger_sync(req: SyncTriggerRequest):
    months = max(1, min(req.months_back, 12)) if req.months_back is not None else None
    log.info("Manual sync triggered via /sync/trigger (months_back=%s)", months)
    if not _start_sync(months):
        return JSONResponse({"status": "already_running"}, status_code=409)
    return JSONResponse({"status": "started"}, status_code=202)


@app.post("/tips/trigger", dependencies=[Depends(require_scheduler_secret)])
def trigger_tips():
    log.info("Manual tip generation triggered via /tips/trigger")
    try:
        run_tips()
        return {"status": "ok"}
    except Exception as e:
        log.error("Manual tip generation failed: %s", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@app.get("/sync/status", dependencies=[Depends(require_scheduler_secret)])
def sync_status():
    from jyske_mcp.slices.finance.storage import Storage
    last = Storage().get_last_sync()
    last_sync = None
    if last is not None:
        last_sync = {
            "started_at": datetime.fromtimestamp(last["started_at"]).isoformat(),
            "completed_at": datetime.fromtimestamp(last["completed_at"]).isoformat(),
            "accounts_synced": last["accounts_synced"],
            "transactions_fetched": last["transactions_fetched"],
            "new_transactions": last["new_transactions"],
            "details": last["errors"],  # contains JSON with per-account info + errors
        }
    return {
        "running": _sync_state["running"],
        "error": _sync_state["error"],
        "last_sync": last_sync,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081)
