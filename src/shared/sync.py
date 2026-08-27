"""Cross-feature transaction boundary for durable sync events."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .access import Identifier


@dataclass(frozen=True, slots=True)
class SyncEventRecord:
    id: str
    aggregate_type: str
    aggregate_id: Identifier
    aggregate_version: int | None
    event_type: str
    payload: object
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ConsistencyFinding:
    issue_type: str
    severity: str
    safe_to_repair: bool = False
    issue_id: str | None = None
    resolved: bool = False


@dataclass(slots=True)
class SyncEventRow:
    """Mutable outbox state passed between the sync domain and its store."""

    id: str
    idempotency_key: str
    aggregate_type: str
    aggregate_id: Identifier
    aggregate_version: int | None
    event_type: str
    payload: object
    canonical_payload: str
    status: str
    occurred_at: datetime
    available_at: datetime | None
    max_retries: int
    failure_count: int = 0
    owner_name: str | None = None
    claim_token: str | None = None
    locked_at: datetime | None = None
    lease_expires_at: datetime | None = None
    processed_at: datetime | None = None
    failed_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class SyncDeliveryAttemptRow:
    id: str
    event_id: str
    attempt_no: int
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class ConsistencyIssueRow:
    id: str
    issue_type: str
    severity: str
    status: str
    safe_to_repair: bool
    created_at: datetime
    updated_at: datetime
    ignored_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SyncOperatorActionRow:
    id: str
    action_type: str
    target_type: str
    target_id: str
    actor_id: Identifier
    occurred_at: datetime
    reason: str | None = None


@dataclass(slots=True)
class ReconciliationRunRow:
    id: str
    mode: str
    cursor: str | None
    status: str
    started_at: datetime
    completed_at: datetime | None = None


class SyncStateStore(Protocol):
    """Persistence-only seam for the sync ledger.

    Filtering, ordering, state validation, retry calculation, and public-error
    mapping remain in ``SyncService``.  A relational adapter only supplies
    transactions, row locks, and explicit row persistence.
    """

    def transaction(self) -> AbstractContextManager[None]: ...

    def insert_event(self, event: SyncEventRow) -> bool:
        """Insert once; return false only for an existing idempotency key."""
        ...

    def get_event(
        self,
        event_id: str,
        *,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> SyncEventRow | None: ...

    def get_event_by_key(
        self, idempotency_key: str, *, for_update: bool = False
    ) -> SyncEventRow | None: ...

    def list_events(self) -> tuple[SyncEventRow, ...]: ...

    def save_event(self, event: SyncEventRow) -> None: ...

    def insert_attempt(self, attempt: SyncDeliveryAttemptRow) -> None: ...

    def list_attempts(self, event_id: str) -> tuple[SyncDeliveryAttemptRow, ...]: ...

    def save_attempt(self, attempt: SyncDeliveryAttemptRow) -> None: ...

    def insert_issue(self, issue: ConsistencyIssueRow) -> None: ...

    def get_issue(
        self, issue_id: str, *, for_update: bool = False
    ) -> ConsistencyIssueRow | None: ...

    def list_issues(self) -> tuple[ConsistencyIssueRow, ...]: ...

    def save_issue(self, issue: ConsistencyIssueRow) -> None: ...

    def insert_action(self, action: SyncOperatorActionRow) -> None: ...

    def insert_reconciliation(self, run: ReconciliationRunRow) -> None: ...

    def save_reconciliation(self, run: ReconciliationRunRow) -> None: ...


class SyncUnitOfWork(Protocol):
    """Commit a handler effect and event completion in one DB transaction."""

    def commit(
        self,
        event: SyncEventRecord,
        mark_processed: Callable[[], None],
    ) -> None: ...


class SyncIndexingEffects(Protocol):
    """Indexing-owned effects committed with sync event completion."""

    def commit_sync_document_deleted(
        self,
        document_id: Identifier,
        mark_processed: Callable[[], None],
    ) -> None: ...

    def commit_sync_document_version_created(
        self,
        document_id: Identifier,
        version_id: Identifier,
        version_no: int,
        mark_processed: Callable[[], None],
    ) -> None: ...

    def commit_sync_document_reindex(
        self,
        version_id: Identifier,
        model_id: Identifier,
        mark_processed: Callable[[], None],
    ) -> None: ...

    def commit_sync_model_activated(
        self,
        model_id: Identifier,
        mark_processed: Callable[[], None],
    ) -> None: ...


class SyncPermissionEffects(Protocol):
    def commit_sync_permission_refresh(
        self,
        event: SyncEventRecord,
        mark_processed: Callable[[], None],
    ) -> None: ...


class ReconciliationDetector(Protocol):
    def detect(
        self,
        *,
        cursor: str | None,
        mode: str,
        limit: int,
    ) -> tuple[ConsistencyFinding, ...]: ...


class TransactionalSyncOutbox(Protocol):
    """Enlist domain state and durable events in one commit boundary."""

    def transaction(self) -> AbstractContextManager[object]: ...


class DocumentSyncOutbox(TransactionalSyncOutbox, Protocol):
    """Document transaction participant that writes the durable sync outbox."""

    def publish_document_version_created(
        self,
        version_id: Identifier,
        version_no: int,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ) -> SyncEventRecord: ...

    def publish_document_deleted(
        self,
        document_id: Identifier,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ) -> SyncEventRecord: ...


class PermissionSyncOutbox(TransactionalSyncOutbox, Protocol):
    """Permission ledger publisher for asynchronous access-cache projection."""

    def publish_permission_cache_refresh(
        self,
        source: str,
        permission_id: Identifier,
        action: str,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ) -> SyncEventRecord: ...
