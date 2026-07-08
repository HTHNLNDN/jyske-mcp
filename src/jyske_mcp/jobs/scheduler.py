import logging
import os
import secrets
from contextlib import asynccontextmanager

# load .env before jyske_mcp.auth (imported transitively below) reads
# os.environ at import time
from dotenv import load_dotenv
from jyske_mcp.config import ENV_FILE
load_dotenv(ENV_FILE)

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from jyske_mcp.jobs.sync import run_sync
from jyske_mcp.jobs.evals import run_evals
from jyske_mcp.jobs.tips import run_tips

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("scheduler")

scheduler = BackgroundScheduler()

# TODO: retired autonomous backlog-refinement job (shelled out to the claude
# CLI from this banking process). If revived it belongs in a separate
# user-level process, never co-located with live-banking sync/evals/tips.


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("SCHEDULER_SECRET"):
        log.warning(
            "SCHEDULER_SECRET is unset/empty — the scheduler will reject all "
            "/sync and /tips trigger requests until it's set."
        )
    scheduler.add_job(run_sync, "cron", hour=3, minute=0, id="daily_sync")
    scheduler.add_job(run_evals, "cron", hour=4, minute=0, id="nightly_evals")
    scheduler.add_job(run_tips, "cron", hour=4, minute=30, id="nightly_tip")
    scheduler.start()
    log.info(
        "Scheduler started — daily sync at 03:00, nightly evals at 04:00, "
        "nightly tip of the day at 04:30"
    )
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


@app.post("/sync/trigger", dependencies=[Depends(require_scheduler_secret)])
def trigger_sync():
    log.info("Manual sync triggered via /sync/trigger")
    try:
        run_sync()
        return {"status": "ok"}
    except Exception as e:
        log.error("Manual sync failed: %s", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


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
    from jyske_mcp.storage import Storage
    last = Storage().get_last_sync()
    if last is None:
        return {"last_sync": None}
    from datetime import datetime
    return {
        "last_sync": {
            "started_at": datetime.fromtimestamp(last["started_at"]).isoformat(),
            "completed_at": datetime.fromtimestamp(last["completed_at"]).isoformat(),
            "accounts_synced": last["accounts_synced"],
            "transactions_fetched": last["transactions_fetched"],
            "new_transactions": last["new_transactions"],
            "details": last["errors"],  # contains JSON with per-account info + errors
        }
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081)
