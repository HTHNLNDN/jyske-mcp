"""
Pydantic DTOs at the kernel storage seam (VSA restructure epic, deliverable
#5 — see .agent/epics/vsa-restructure-blueprint.md §2/§3).

These model the generic-primitive shapes KernelStorage (jyske_mcp/storage.py)
exchanges with its callers: merchant categorization, sync bookkeeping,
agents, session summaries, and compact (never raw_data) transaction rows.

Opaque by design (NOT modeled here, per the blueprint's explicit carve-out):
cache blobs (cache_get/cache_set), user_profile JSON, and raw Enable Banking
transaction dicts (get_transactions_cached/store_transaction). Session and
balance data extend that same carve-out for a concrete, test-proven reason:
Storage.get_session()/read_session_unchecked()/save_session() and
store_balance()/get_balances_cached() must keep returning/accepting the raw
EB-shaped dict verbatim — tests/test_consent_flow.py pins byte-for-byte
round-trip equality on both (saved session accounts, remapped balance data),
which a lossy typed projection would break. AccountDTO/SessionDTO/
BalanceLineDTO/BalanceSnapshotDTO below are still real, used types: they're
built by callers (mcp/server.py's list_accounts/get_balances) FROM that raw
dict for typed, tolerant read access, via explicit from_raw() keyword
extraction — never `Model(**raw_dict)` — so an Enable Banking field this
codebase doesn't otherwise use can never trip extra="forbid".
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AccountDTO(BaseModel):
    """Typed, tolerant view of one entry from a session's `accounts` list
    (Enable Banking's AccountResource shape, trimmed to the fields this
    codebase actually reads). Never round-tripped back into
    Storage.save_session — see module docstring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    uid: str | None = None
    product: str | None = None
    currency: str | None = None
    iban: str | None = None
    identification_hash: str | None = None

    @classmethod
    def from_raw(cls, acc: dict[str, Any]) -> "AccountDTO":
        return cls(
            uid=acc.get("uid"),
            product=acc.get("product"),
            currency=acc.get("currency"),
            iban=(acc.get("account_id") or {}).get("iban"),
            identification_hash=acc.get("identification_hash"),
        )


class SessionDTO(BaseModel):
    """Typed view of the session.json payload. Storage.get_session()/
    read_session_unchecked() keep returning the raw dict (see module
    docstring) — this is built by callers via from_raw() when they want
    typed access instead of dict .get() calls."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str | None = None
    valid_until: str | None = None
    accounts: list[AccountDTO] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, session: dict[str, Any]) -> "SessionDTO":
        return cls(
            session_id=session.get("session_id"),
            valid_until=session.get("valid_until"),
            accounts=[AccountDTO.from_raw(a) for a in session.get("accounts", [])],
        )


class BalanceLineDTO(BaseModel):
    """One entry from a cached balance snapshot's `balances` array (Enable
    Banking's per-account balances response)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    balance_type: str | None = None
    amount: str | None = None
    currency: str | None = None

    @classmethod
    def from_raw(cls, b: dict[str, Any]) -> "BalanceLineDTO":
        amt = b.get("balance_amount") or {}
        return cls(
            balance_type=b.get("balance_type"),
            amount=amt.get("amount"),
            currency=amt.get("currency"),
        )


class BalanceSnapshotDTO(BaseModel):
    """Typed view of one account's cached balance data. Storage.store_balance/
    get_balances_cached/balance_fetched_at keep returning the raw EB dict
    unchanged (see module docstring) — this is built by callers from that
    raw dict plus Storage.balance_fetched_at()'s timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_uid: str
    fetched_at: float | None = None
    balances: list[BalanceLineDTO] = Field(default_factory=list)

    @classmethod
    def from_raw(
        cls, account_uid: str, data: dict[str, Any] | None, fetched_at: float | None
    ) -> "BalanceSnapshotDTO":
        raw_balances = (data or {}).get("balances", [])
        return cls(
            account_uid=account_uid,
            fetched_at=fetched_at,
            balances=[BalanceLineDTO.from_raw(b) for b in raw_balances],
        )


class MerchantCategoryDTO(BaseModel):
    """A resolved (or user/LLM/MCC-assigned) merchant category — the shape
    KernelStorage.merchant_get() returns and jyske_mcp.kernel.categorizer.categorize()
    passes through/constructs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category_top: str | None = None
    category_mid: str | None = None
    category_leaf: str | None = None
    resolved_name: str | None = None
    mcc: str | None = None
    source: str | None = None


class SyncRecordDTO(BaseModel):
    """One row from the `syncs` bookkeeping table (KernelStorage.get_last_sync/
    record_sync)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: float | None = None
    completed_at: float | None = None
    accounts_synced: int | None = None
    transactions_fetched: int | None = None
    new_transactions: int | None = None
    errors: str | None = None


class AgentDTO(BaseModel):
    """One row from the `agents` registry table (KernelStorage.get_agents/
    get_agent)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str | None = None
    model: str | None = None
    created_at: float
    updated_at: float


class SummaryDTO(BaseModel):
    """One row from `session_summaries` (KernelStorage.get_all_summaries)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    created_at: float


class TransactionRowDTO(BaseModel):
    """One compact transaction row — id/typed columns only, NEVER raw_data
    (KernelStorage.get_all_transactions; see the no-raw-transaction-data
    rule in web/app.py's /audit/data)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    account_uid: str
    transaction_id: str | None = None
    date: str
    amount: float | None = None
    currency: str | None = None
    description: str | None = None
    mcc: str | None = None
    category_top: str | None = None
    category_mid: str | None = None
    category_leaf: str | None = None
    direction: str | None = None
    created_at: float
