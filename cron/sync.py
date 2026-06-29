import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# project root on path so lib/ imports work regardless of CWD
sys.path.insert(0, str(Path(__file__).parent.parent))

# load .env before lib/auth.py reads os.environ at import time
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import anthropic
import requests

from lib.auth import auth_headers, BASE_URL
from lib.categorizer import categorize
from lib.storage import Storage, SessionExpiredError

# ── logging ───────────────────────────────────────────────────────────────────

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
log = logging.getLogger("sync")

# ── constants ─────────────────────────────────────────────────────────────────

_BALANCE_TTL = 6 * 3600   # skip balance fetch if last one was less than 6h ago
_BASELINE_DAYS = 90        # how far back to go on first run


# ── helpers ───────────────────────────────────────────────────────────────────

def _most_recent_tx_date(storage: Storage, account_uid: str) -> str | None:
    """Return the most recent booking_date stored in the transactions table for this account."""
    rows = storage.get_transactions_cached(account_uid, "0000-01-01", "9999-12-31")
    if not rows:
        return None
    tx = rows[0]
    return tx.get("booking_date") or tx.get("value_date")


def _balance_stale(storage: Storage, account_uid: str) -> bool:
    """True if no balance data cached or last fetch was more than 6 hours ago."""
    fetched_at = storage.balance_fetched_at(account_uid)
    return fetched_at is None or (time.time() - fetched_at) >= _BALANCE_TTL


def _batch_categorize(items: list[dict], storage: Storage) -> None:
    """
    Single Anthropic call to categorize a deduplicated batch of merchants.
    Uses Haiku — fast and cheap for a straightforward classification task.
    Stores each result via storage.merchant_set.
    """
    if not items:
        return

    merchants_json = json.dumps(
        [{"raw_name": i["raw_name"], "mcc": i.get("mcc")} for i in items],
        ensure_ascii=False,
    )

    prompt = (
        "Categorize each merchant in the list below. "
        "Return ONLY a JSON array — one object per merchant, same order as input. "
        'Each object: {"raw_name": "...", "top": "...", "mid": "...", "leaf": "..."}\n\n'
        "Use exactly these top-level categories:\n"
        "Food & Dining, Shopping, Transport, Travel, Health & Wellness, Entertainment, "
        "Home & Utilities, Finance & Insurance, Education, Personal Services, "
        "Professional & Business Services, Government & Non-profit, Other\n\n"
        f"Merchants:\n{merchants_json}"
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        results = json.loads(text)
        for item in results:
            storage.merchant_set(
                raw_name=item["raw_name"],
                category_top=item["top"],
                category_mid=item["mid"],
                category_leaf=item["leaf"],
                source="llm",
            )
        log.info("LLM categorized %d merchants", len(results))
    except Exception as e:
        log.error("Batch categorization failed: %s", e)


# ── main sync ─────────────────────────────────────────────────────────────────

def run_sync() -> None:
    started_at = time.time()
    log.info("─── Sync started ───────────────────────────────────────────")

    storage = Storage()
    errors: list[str] = []
    account_details: list[dict] = []
    total_fetched = 0
    total_new = 0
    accounts_synced = 0
    needs_llm: list[dict] = []

    try:
        session = storage.get_session()
    except SessionExpiredError as e:
        log.error("Session error — %s", e)
        storage.record_sync(started_at, time.time(), 0, 0, 0, str(e))
        return

    accounts = session.get("accounts", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for acc in accounts:
        uid = acc["uid"]
        product = acc.get("product", uid)

        # ── transactions ──────────────────────────────────────────────────────

        most_recent = _most_recent_tx_date(storage, uid)

        if most_recent:
            # one day overlap: catches transactions that settled or were backdated
            date_from = (
                datetime.strptime(most_recent, "%Y-%m-%d") - timedelta(days=1)
            ).strftime("%Y-%m-%d")
            log.info("%s: incremental  %s → %s  (last seen: %s)", product, date_from, today, most_recent)
        else:
            date_from = (
                datetime.now(timezone.utc) - timedelta(days=_BASELINE_DAYS)
            ).strftime("%Y-%m-%d")
            log.info("%s: first run, baseline %d days  %s → %s", product, _BASELINE_DAYS, date_from, today)

        try:
            r = requests.get(
                f"{BASE_URL}/accounts/{uid}/transactions",
                headers=auth_headers(),
                params={"date_from": date_from, "date_to": today},
            )
            if r.status_code == 401:
                raise SessionExpiredError("API 401 — re-run setup_consent.py")
            r.raise_for_status()
            data = r.json()
        except SessionExpiredError as e:
            log.error("%s: %s", product, e)
            errors.append(f"{product}: {e}")
            continue
        except Exception as e:
            log.error("%s: transaction fetch failed: %s", product, e)
            errors.append(f"{product} transactions: {e}")
            continue

        transactions = data.get("transactions", [])
        fetched = len(transactions)
        total_fetched += fetched

        # new = booking_date strictly after the previous most-recent date
        new_count = sum(
            1 for tx in transactions
            if (tx.get("booking_date") or tx.get("value_date", "")) > (most_recent or "")
        )
        total_new += new_count

        for tx in transactions:
            storage.store_transaction(uid, tx)
        log.info("%s: %d fetched, %d new, %d already had", product, fetched, new_count, fetched - new_count)

        # categorize only genuinely new transactions
        for tx in transactions:
            tx_date = tx.get("booking_date") or tx.get("value_date", "")
            if tx_date <= (most_recent or ""):
                continue
            raw_name = (
                tx.get("creditor_name")
                or (tx.get("remittance_information") or [""])[0]
                or tx.get("debtor_name", "")
            )
            if not raw_name:
                continue
            mcc = tx.get("mcc") or tx.get("merchant_category_code")
            if categorize(raw_name, mcc, storage) is None:
                needs_llm.append({"raw_name": raw_name, "mcc": mcc})

        # ── balances ──────────────────────────────────────────────────────────

        if _balance_stale(storage, uid):
            try:
                r = requests.get(
                    f"{BASE_URL}/accounts/{uid}/balances",
                    headers=auth_headers(),
                )
                if r.status_code == 401:
                    raise SessionExpiredError("API 401")
                r.raise_for_status()
                storage.store_balance(uid, r.json())
                log.info("%s: balances refreshed", product)
            except Exception as e:
                log.error("%s: balance fetch failed: %s", product, e)
                errors.append(f"{product} balances: {e}")
        else:
            log.info("%s: balances fresh (<6h), skipping", product)

        accounts_synced += 1
        account_details.append({
            "uid": uid,
            "product": product,
            "date_from": date_from,
            "date_to": today,
            "fetched": fetched,
            "new": new_count,
        })

    # ── batch LLM categorization ──────────────────────────────────────────────

    # deduplicate by raw_name — no point sending the same merchant twice
    seen: set[str] = set()
    unique_needs_llm = [
        item for item in needs_llm
        if item["raw_name"] not in seen and not seen.add(item["raw_name"])
    ]

    if unique_needs_llm:
        log.info("Sending %d unknown merchants to LLM for categorization", len(unique_needs_llm))
        _batch_categorize(unique_needs_llm, storage)
    else:
        log.info("No unknown merchants — categorization skipped")

    # ── record ────────────────────────────────────────────────────────────────

    completed_at = time.time()
    details_payload = json.dumps({"accounts": account_details, "errors": errors})

    storage.record_sync(
        started_at=started_at,
        completed_at=completed_at,
        accounts_synced=accounts_synced,
        transactions_fetched=total_fetched,
        new_transactions=total_new,
        errors=details_payload,
    )

    log.info(
        "Done in %.1fs — %d accounts, %d fetched (%d new), %d LLM-categorized, %d errors",
        completed_at - started_at,
        accounts_synced,
        total_fetched,
        total_new,
        len(unique_needs_llm),
        len(errors),
    )


if __name__ == "__main__":
    run_sync()
