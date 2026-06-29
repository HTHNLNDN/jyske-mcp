import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# project root and cron/ dir on path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

from sync import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("scheduler")

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(run_sync, "cron", hour=3, minute=0, id="daily_sync")
    scheduler.start()
    log.info("Scheduler started — daily sync at 03:00")
    yield
    scheduler.shutdown()
    log.info("Scheduler stopped")


app = FastAPI(lifespan=lifespan)


@app.post("/sync/trigger")
def trigger_sync():
    log.info("Manual sync triggered via /sync/trigger")
    try:
        run_sync()
        return {"status": "ok"}
    except Exception as e:
        log.error("Manual sync failed: %s", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@app.get("/sync/status")
def sync_status():
    from lib.storage import Storage
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
    uvicorn.run(app, host="0.0.0.0", port=8081)
