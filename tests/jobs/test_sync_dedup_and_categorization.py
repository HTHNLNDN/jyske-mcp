"""
Fills genuine gaps in this directory's coverage of jyske_mcp/kernel/sync.py's
run_sync(): which fetched transactions get queued for LLM categorization
(the categorize_all / "tx_date <= most_recent" incremental-skip gating) and
the raw_name dedup gate before _batch_categorize is ever called. Read
first, not duplicated here: test_sync_pagination.py (pagination
accumulation/termination), test_sync_timeout.py (HTTP_TIMEOUT wiring),
test_sync_single_owner.py (concurrent-trigger rejection),
test_sync_freshness.py (is_sync_stale/check_sync_freshness), and
test_scheduler_auth.py (the X-Scheduler-Secret guard) — none of those touch
categorization or the needs_llm dedup step.

_batch_categorize itself (the actual LLM call, via simple_completion) is
mocked out in every test below — these tests only pin what run_sync decides
to feed it, never what it does with that input, so no LLM call happens
anywhere in this file.
"""
import time
from unittest.mock import MagicMock

import jyske_mcp.kernel.sync as sync


def _fake_response(*, transactions=None, continuation_key=None, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json.return_value = {
        "transactions": transactions if transactions is not None else [],
        "continuation_key": continuation_key,
    }
    return r


def _stub_storage(*, most_recent):
    storage = MagicMock()
    storage.get_session.return_value = {"accounts": [{"uid": "acc1", "product": "Test"}]}
    storage.most_recent_transaction_date.return_value = most_recent
    storage.balance_fetched_at.return_value = time.time()  # fresh -> balances GET skipped
    storage.get_budget_status.return_value = []
    storage.backfill_categories.return_value = 0
    return storage


def test_incremental_sync_skips_categorizing_transactions_at_or_before_cursor(monkeypatch, patched_auth_headers):
    txs = [
        {"transaction_id": "old", "booking_date": "2026-07-08", "creditor_name": "Old Shop"},
        {"transaction_id": "new", "booking_date": "2026-07-10", "creditor_name": "New Shop"},
    ]
    monkeypatch.setattr(sync._EB_SESSION, "get", MagicMock(
        return_value=_fake_response(transactions=txs, continuation_key=None)
    ))
    # Force every merchant to look "unknown" so the categorize_all/skip
    # gating logic is the only thing deciding what reaches needs_llm.
    monkeypatch.setattr(sync, "categorize", lambda raw_name, mcc, storage: None)
    spy = MagicMock()
    monkeypatch.setattr(sync, "_batch_categorize", spy)
    monkeypatch.setattr(sync, "KernelStorage", MagicMock(return_value=_stub_storage(most_recent="2026-07-08")))

    sync.run_sync()  # months_back=None -> categorize_all=False

    spy.assert_called_once()
    queued = {item["raw_name"] for item in spy.call_args.args[0]}
    assert queued == {"New Shop"}  # "Old Shop" (booking_date <= cursor) excluded


def test_backfill_sync_categorizes_transactions_older_than_cursor_too(monkeypatch, patched_auth_headers):
    txs = [
        {"transaction_id": "old", "booking_date": "2026-07-08", "creditor_name": "Old Shop"},
        {"transaction_id": "new", "booking_date": "2026-07-10", "creditor_name": "New Shop"},
    ]
    monkeypatch.setattr(sync._EB_SESSION, "get", MagicMock(
        return_value=_fake_response(transactions=txs, continuation_key=None)
    ))
    monkeypatch.setattr(sync, "categorize", lambda raw_name, mcc, storage: None)
    spy = MagicMock()
    monkeypatch.setattr(sync, "_batch_categorize", spy)
    monkeypatch.setattr(sync, "KernelStorage", MagicMock(return_value=_stub_storage(most_recent="2026-07-08")))

    sync.run_sync(months_back=3)  # explicit backfill -> categorize_all=True

    spy.assert_called_once()
    queued = {item["raw_name"] for item in spy.call_args.args[0]}
    assert queued == {"Old Shop", "New Shop"}


def test_needs_llm_deduplicated_by_raw_name_before_batch_categorize(monkeypatch, patched_auth_headers):
    txs = [
        {"transaction_id": "a", "booking_date": "2026-07-10", "creditor_name": "Same Shop"},
        {"transaction_id": "b", "booking_date": "2026-07-10", "creditor_name": "Same Shop"},
        {"transaction_id": "c", "booking_date": "2026-07-10", "creditor_name": "Other Shop"},
    ]
    monkeypatch.setattr(sync._EB_SESSION, "get", MagicMock(
        return_value=_fake_response(transactions=txs, continuation_key=None)
    ))
    monkeypatch.setattr(sync, "categorize", lambda raw_name, mcc, storage: None)
    spy = MagicMock()
    monkeypatch.setattr(sync, "_batch_categorize", spy)
    monkeypatch.setattr(sync, "KernelStorage", MagicMock(return_value=_stub_storage(most_recent=None)))  # first run

    sync.run_sync()

    spy.assert_called_once()
    items = spy.call_args.args[0]
    assert len(items) == 2  # "Same Shop" sent once despite 2 transactions
    assert {i["raw_name"] for i in items} == {"Same Shop", "Other Shop"}


def test_no_unknown_merchants_skips_batch_categorize_call_entirely(monkeypatch, patched_auth_headers):
    txs = [{"transaction_id": "a", "booking_date": "2026-07-10", "creditor_name": "Known Shop"}]
    monkeypatch.setattr(sync._EB_SESSION, "get", MagicMock(
        return_value=_fake_response(transactions=txs, continuation_key=None)
    ))
    monkeypatch.setattr(sync, "categorize", lambda raw_name, mcc, storage: {
        "category_top": "Other", "category_mid": "Other", "category_leaf": "Other",
    })
    spy = MagicMock()
    monkeypatch.setattr(sync, "_batch_categorize", spy)
    monkeypatch.setattr(sync, "KernelStorage", MagicMock(return_value=_stub_storage(most_recent=None)))

    sync.run_sync()

    spy.assert_not_called()
