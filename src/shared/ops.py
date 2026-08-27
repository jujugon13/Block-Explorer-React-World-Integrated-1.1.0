"""Cross-feature values and external ports for the operations dashboard."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OpsDocumentSnapshot:
    status: str
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OpsJobSnapshot:
    status: str
    first_started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OpsWorkerSnapshot:
    status: str
    last_heartbeat: datetime


@dataclass(frozen=True, slots=True)
class OpsSearchSnapshot:
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class OpsSnapshot:
    documents: tuple[OpsDocumentSnapshot, ...] = ()
    jobs: tuple[OpsJobSnapshot, ...] = ()
    workers: tuple[OpsWorkerSnapshot, ...] = ()
    searches: tuple[OpsSearchSnapshot, ...] = ()


class OpsSnapshotReader(Protocol):
    """Read one relational snapshot for all dashboard counters."""

    def read_ops_snapshot(self, now: datetime) -> OpsSnapshot: ...


class OpsDocumentSnapshotSource(Protocol):
    """Project document state without exposing document content."""

    def ops_document_snapshots(
        self, now: datetime
    ) -> tuple[OpsDocumentSnapshot, ...]: ...


class OpsIndexingSnapshotSource(Protocol):
    """Project indexing jobs and effective worker state."""

    def ops_job_snapshots(self, now: datetime) -> tuple[OpsJobSnapshot, ...]: ...

    def ops_worker_snapshots(self, now: datetime) -> tuple[OpsWorkerSnapshot, ...]: ...


class OpsSearchSnapshotSource(Protocol):
    """Project search request instants without queries or answers."""

    def ops_search_snapshots(self, now: datetime) -> tuple[OpsSearchSnapshot, ...]: ...


class DashboardPublisher(Protocol):
    """Port implemented by the configured STOMP/WebSocket broker adapter."""

    def publish(self, destination: str, payload: Mapping[str, object]) -> None: ...


class StompMessageBroker(Protocol):
    """Raw broker boundary used by a dashboard JSON publisher."""

    def publish(self, destination: str, body: bytes, content_type: str) -> int: ...


class OpsIndexingCommands(Protocol):
    """Operator retry boundary implemented by the indexing domain."""

    def manual_retry(self, job_id: int, now: datetime | None = None) -> object: ...

    def retry_all(
        self,
        now: datetime | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> object: ...
