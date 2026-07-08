"""
First pytest suite for the repo — covers the Enable Banking transaction
pagination fix in jyske_mcp/jobs/sync.py.

Contract under test (research/briefs/enable-banking-transactions-pagination-2026-07-07.md):
  - the next-page token is a top-level string field `continuation_key`
  - it is re-sent as a GET query param alongside the original, unchanged
    date_from/date_to
  - pagination is exhausted when `continuation_key` is absent/null — NOT
    when the `transactions` array on a page happens to be empty
"""
from unittest.mock import MagicMock

import jyske_mcp.jobs.sync as sync


def _fake_response(*, transactions=None, continuation_key=None, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    r.json.return_value = {
        "transactions": transactions if transactions is not None else [],
        "continuation_key": continuation_key,
    }
    return r


def test_fetch_transactions_accumulates_all_pages_stops_on_null_key(monkeypatch, patched_auth_headers):
    page1 = _fake_response(transactions=[{"id": "a"}, {"id": "b"}], continuation_key="k1")
    page2 = _fake_response(transactions=[], continuation_key="k2")  # empty but MUST continue
    page3 = _fake_response(transactions=[{"id": "c"}], continuation_key=None)

    mock_get = MagicMock(side_effect=[page1, page2, page3])
    monkeypatch.setattr(sync.requests, "get", mock_get)

    transactions, page_counts, truncated = sync._fetch_transactions(
        "acc1", "2026-01-01", "2026-07-07"
    )

    assert [tx["id"] for tx in transactions] == ["a", "b", "c"]
    assert page_counts == [2, 0, 1]
    assert truncated is None
    assert mock_get.call_count == 3

    # 2nd and 3rd calls carried the continuation_key from the previous page,
    # with date_from/date_to unchanged.
    _, kwargs2 = mock_get.call_args_list[1]
    _, kwargs3 = mock_get.call_args_list[2]
    assert kwargs2["params"] == {
        "date_from": "2026-01-01", "date_to": "2026-07-07", "continuation_key": "k1",
    }
    assert kwargs3["params"] == {
        "date_from": "2026-01-01", "date_to": "2026-07-07", "continuation_key": "k2",
    }


def test_run_sync_stores_all_pages_via_storage(monkeypatch, patched_auth_headers):
    page1 = _fake_response(transactions=[{"transaction_id": "a"}, {"transaction_id": "b"}], continuation_key="k1")
    page2 = _fake_response(transactions=[{"transaction_id": "c"}], continuation_key=None)

    mock_get = MagicMock(side_effect=[page1, page2])
    monkeypatch.setattr(sync.requests, "get", mock_get)
    monkeypatch.setattr(sync, "categorize", lambda raw_name, mcc, storage: {
        "top": "Other", "mid": "Other", "leaf": "Other",
    })

    storage = MagicMock()
    storage.get_session.return_value = {"accounts": [{"uid": "acc1", "product": "Test"}]}
    storage.most_recent_transaction_date.return_value = None  # else MagicMock() is truthy, defeating first-run branch
    storage.balance_fetched_at.return_value = __import__("time").time()  # balances fresh -> GET skipped
    storage.get_budget_status.return_value = []
    storage.backfill_categories.return_value = 0
    monkeypatch.setattr(sync, "Storage", MagicMock(return_value=storage))

    sync.run_sync()

    # sync now stores all pages for an account in a single batch call rather
    # than one storage.store_transaction call per row.
    assert storage.store_transactions_batch.call_count == 1
    stored_ids = {tx["transaction_id"] for tx in storage.store_transactions_batch.call_args.args[1]}
    assert stored_ids == {"a", "b", "c"}


def test_fetch_transactions_preserves_progress_on_mid_loop_429(monkeypatch, patched_auth_headers):
    page1 = _fake_response(transactions=[{"id": "a"}], continuation_key="k1")
    page2 = _fake_response(status_code=429)

    mock_get = MagicMock(side_effect=[page1, page2])
    monkeypatch.setattr(sync.requests, "get", mock_get)

    transactions, page_counts, truncated = sync._fetch_transactions(
        "acc1", "2026-01-01", "2026-07-07"
    )

    assert [tx["id"] for tx in transactions] == ["a"]
    assert page_counts == [1]
    assert truncated is not None
    assert "rate-limit" in truncated.lower() or "429" in truncated
