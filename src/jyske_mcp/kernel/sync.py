import json
import logging
import time
from datetime import datetime, timezone, timedelta

# load .env before jyske_mcp/kernel/auth.py reads os.environ at import time
from dotenv import load_dotenv
from jyske_mcp.kernel.config import ENV_FILE, SYNC_LOG_FILE, secure_config_files, secure_rotating_handler
load_dotenv(ENV_FILE)

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from jyske_mcp.kernel.auth import auth_headers, BASE_URL, HTTP_TIMEOUT
from jyske_mcp.kernel.categorizer import categorize
from jyske_mcp.kernel.llm import simple_completion
from jyske_mcp.kernel.storage import KernelStorage, SessionExpiredError

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        secure_rotating_handler(SYNC_LOG_FILE, "%(asctime)s  %(levelname)-8s  %(message)s"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sync")

# ── constants ─────────────────────────────────────────────────────────────────

_BALANCE_TTL = 6 * 3600   # skip balance fetch if last one was less than 6h ago
_BASELINE_DAYS = 90        # how far back to go on first run
STALE_SYNC_HOURS = 26


def _build_eb_session() -> requests.Session:
    """
    Session with automatic retry/backoff for transient network and ASPSP
    failures. 429 and 401 are deliberately NOT in status_forcelist — they
    must fall through to the existing per-status handling in
    _fetch_transactions / run_sync (429 -> truncate-and-resume, 401 ->
    SessionExpiredError) rather than being retried/consumed here.
    """
    retry = Retry(
        total=3,
        connect=2,
        read=1,
        status=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        backoff_factor=0.5,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_EB_SESSION = _build_eb_session()


# ── helpers ───────────────────────────────────────────────────────────────────

def is_sync_stale(last_sync: dict | None, now: float, threshold_hours: float = STALE_SYNC_HOURS) -> bool:
    """True if no sync has ever completed, or the last one finished more
    than `threshold_hours` ago (a possible sign the sync is wedged)."""
    if last_sync is None:
        return True
    return (now - last_sync["completed_at"]) > threshold_hours * 3600

def _balance_stale(storage: KernelStorage, account_uid: str) -> bool:
    """True if no balance data cached or last fetch was more than 6 hours ago."""
    fetched_at = storage.balance_fetched_at(account_uid)
    return fetched_at is None or (time.time() - fetched_at) >= _BALANCE_TTL


def _batch_categorize(items: list[dict], storage: KernelStorage) -> None:
    """
    Single LLM call to categorize a deduplicated batch of merchants.
    Hardcoded to Haiku regardless of LLM_MODEL — fast and cheap for a
    straightforward classification task, and this is a cost-sensitive
    background job rather than the user-facing chat model.
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
        "Professional & Business Services, Government & Non-profit, Agriculture & Industry, Other\n\n"
        f"Merchants:\n{merchants_json}"
    )

    try:
        text = simple_completion(prompt, model="claude-haiku-4-5-20251001").strip()
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


def _fetch_transactions(
    uid: str, date_from: str, date_to: str, *, max_pages: int = 50
) -> tuple[list[dict], list[int], str | None]:
    """
    Fetch all pages of transactions for an account, following Enable Banking's
    top-level `continuation_key` field. Termination is `not continuation_key`
    — NEVER an empty-page short-circuit, since an empty page can still carry
    a continuation key.

    Raises SessionExpiredError on HTTP 401 on any page (session-scoped key is
    dead after re-auth, so it's never worth preserving partial progress there).
    On a 429 (ASPSP rate limit) or any error after page 1, partial progress is
    preserved and returned with a `truncated` reason instead of raising —
    pagination simply stops for this run and resumes on the next one.

    Returns (transactions, page_counts, truncated_reason).
    """
    url = f"{BASE_URL}/accounts/{uid}/transactions"
    base_params = {"date_from": date_from, "date_to": date_to}

    transactions: list[dict] = []
    page_counts: list[int] = []
    continuation: str | None = None
    truncated: str | None = None

    while True:
        params = dict(base_params)
        if continuation:
            params["continuation_key"] = continuation
        try:
            r = _EB_SESSION.get(url, headers=auth_headers(), params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 401:
                raise SessionExpiredError("EXPIRED_SESSION — API 401 on transaction fetch")
            if r.status_code == 429:
                truncated = f"rate-limited (429) after {len(page_counts)} page(s); resumes next run"
                break
            r.raise_for_status()
            data = r.json()
        except SessionExpiredError:
            raise
        except Exception as e:
            if not page_counts:
                raise  # page-1 failure → caller's generic except
            truncated = f"aborted after {len(page_counts)} page(s): {e}"
            break

        page = data.get("transactions", [])
        transactions.extend(page)
        page_counts.append(len(page))

        continuation = data.get("continuation_key")
        if not continuation:
            break  # THE exhaustion signal — never len(page) == 0
        if len(page_counts) >= max_pages:
            truncated = f"hit max_pages={max_pages} with continuation_key still present"
            break

    return transactions, page_counts, truncated


# ── main sync ─────────────────────────────────────────────────────────────────

def run_sync(months_back: int | None = None) -> None:
    secure_config_files()
    started_at = time.time()
    log.info("─── Sync started ───────────────────────────────────────────")

    storage = KernelStorage()
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
    today_dt = datetime.now(timezone.utc)
    today = today_dt.strftime("%Y-%m-%d")

    # In backfill mode (months_back given), fetch may return genuinely-new
    # transactions older than the incremental cursor, so the usual
    # tx_date > most_recent optimization would silently skip categorizing
    # them. Force every newly-stored transaction through categorization in
    # that case — the merchant cache keeps re-checks cheap.
    categorize_all = months_back is not None

    for acc in accounts:
        uid = acc["uid"]
        product = acc.get("product", uid)

        # ── transactions ──────────────────────────────────────────────────────

        most_recent = storage.most_recent_transaction_date(uid)

        if months_back is not None:
            # explicit backfill request takes precedence over the incremental
            # cursor, even for accounts that already have stored data.
            date_from = (today_dt - timedelta(days=months_back * 31)).strftime("%Y-%m-%d")
            log.info("%s: backfill %d month(s)  %s → %s", product, months_back, date_from, today)
        elif most_recent:
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
            transactions, page_counts, truncated = _fetch_transactions(uid, date_from, today)
        except SessionExpiredError as e:
            log.error("%s: %s", product, e)
            errors.append(f"{product}: {e}")
            continue
        except Exception as e:
            log.error("%s: transaction fetch failed: %s", product, e)
            errors.append(f"{product} transactions: {e}")
            continue

        pages = len(page_counts)
        fetched = len(transactions)
        total_fetched += fetched

        # page-count sanity invariant — log-and-continue, never hard-fail the run
        if fetched != sum(page_counts):
            msg = f"page-count mismatch: {fetched} accumulated vs {sum(page_counts)} summed across {pages} page(s)"
            log.error("%s: %s", product, msg)
            errors.append(f"{product} transactions: {msg}")
        if truncated:
            log.warning("%s: pagination incomplete — %s", product, truncated)
            errors.append(f"{product} transactions: {truncated}")

        # new = booking_date strictly after the previous most-recent date
        new_count = sum(
            1 for tx in transactions
            if (tx.get("booking_date") or tx.get("value_date", "")) > (most_recent or "")
        )
        total_new += new_count

        try:
            storage.store_transactions_batch(uid, transactions)
        except Exception as e:
            log.error("%s: storing transactions failed: %s", product, e)
            errors.append(f"{product} transactions: store failed: {e}")
            continue
        log.info(
            "%s: %d fetched across %d page(s), %d new, %d already had",
            product, fetched, pages, new_count, fetched - new_count,
        )

        # categorize genuinely new transactions — in backfill mode
        # (categorize_all) also include newly-stored transactions older than
        # `most_recent`, since backfill can fetch real history predating the
        # existing cursor.
        for tx in transactions:
            tx_date = tx.get("booking_date") or tx.get("value_date", "")
            if not categorize_all and tx_date <= (most_recent or ""):
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
                r = _EB_SESSION.get(
                    f"{BASE_URL}/accounts/{uid}/balances",
                    headers=auth_headers(),
                    timeout=HTTP_TIMEOUT,
                )
                if r.status_code == 401:
                    raise SessionExpiredError("EXPIRED_SESSION — API 401 on balance fetch")
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
            "pages": pages,
            "truncated": truncated,
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

    # Backfill any transaction rows that still lack a category now that the
    # merchants table above may have just gained new entries.
    backfilled = storage.backfill_categories()
    if backfilled:
        log.info("Backfilled category columns on %d transaction row(s)", backfilled)

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

    # Budget-history snapshotting used to happen here. It's finance-domain
    # (get_budget_status/record_budget_history), so it's lifted out into
    # jyske_mcp.slices.finance.api.snapshot_budget_history() — kernel must
    # never call finance storage (see .agent/epics/vsa-restructure-blueprint.md's
    # "Two cross-layer couplings"). The platform scheduler's daily_sync job
    # calls run_sync() then that hook, in the same job, immediately after this
    # function returns.

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
