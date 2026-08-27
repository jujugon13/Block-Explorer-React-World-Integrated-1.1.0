"""Composition adapter for the operations dashboard source projections."""

from __future__ import annotations

from datetime import datetime

from src.shared import (
    OpsDocumentSnapshot,
    OpsDocumentSnapshotSource,
    OpsIndexingSnapshotSource,
    OpsJobSnapshot,
    OpsSearchSnapshot,
    OpsSearchSnapshotSource,
    OpsSnapshot,
    OpsWorkerSnapshot,
)


_INVALID_SOURCE = "operations snapshot source returned invalid data"


def _aware(moment: object) -> None:
    if (
        not isinstance(moment, datetime)
        or moment.tzinfo is None
        or moment.utcoffset() is None
    ):
        raise ValueError("operations instants must be timezone-aware")


def _validate_documents(rows: tuple[object, ...]) -> None:
    for row in rows:
        if not isinstance(row, OpsDocumentSnapshot) or not isinstance(row.status, str):
            raise TypeError(_INVALID_SOURCE)
        if row.deleted_at is not None:
            _aware(row.deleted_at)


def _validate_jobs(rows: tuple[object, ...]) -> None:
    for row in rows:
        if not isinstance(row, OpsJobSnapshot) or not isinstance(row.status, str):
            raise TypeError(_INVALID_SOURCE)
        if row.first_started_at is not None:
            _aware(row.first_started_at)
        if row.completed_at is not None:
            _aware(row.completed_at)


def _validate_workers(rows: tuple[object, ...]) -> None:
    for row in rows:
        if not isinstance(row, OpsWorkerSnapshot) or not isinstance(row.status, str):
            raise TypeError(_INVALID_SOURCE)
        _aware(row.last_heartbeat)


def _validate_searches(rows: tuple[object, ...]) -> None:
    for row in rows:
        if not isinstance(row, OpsSearchSnapshot):
            raise TypeError(_INVALID_SOURCE)
        _aware(row.requested_at)


class CompositeOpsSnapshotReader:
    """Combine feature-owned projections into one immutable dashboard snapshot."""

    def __init__(
        self,
        documents: OpsDocumentSnapshotSource,
        indexing: OpsIndexingSnapshotSource,
        searches: OpsSearchSnapshotSource,
    ) -> None:
        self._documents = documents
        self._indexing = indexing
        self._searches = searches

    def read_ops_snapshot(self, now: datetime) -> OpsSnapshot:
        _aware(now)
        try:
            documents = tuple(self._documents.ops_document_snapshots(now))
            jobs = tuple(self._indexing.ops_job_snapshots(now))
            workers = tuple(self._indexing.ops_worker_snapshots(now))
            searches = tuple(self._searches.ops_search_snapshots(now))
        except Exception:
            raise RuntimeError("operations snapshot unavailable") from None

        _validate_documents(documents)
        _validate_jobs(jobs)
        _validate_workers(workers)
        _validate_searches(searches)
        return OpsSnapshot(documents, jobs, workers, searches)
