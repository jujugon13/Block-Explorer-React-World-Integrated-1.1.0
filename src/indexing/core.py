"""Lease-owned, idempotent document indexing state machine."""

from __future__ import annotations

import random
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from src.shared import DocumentIndexLedger
from src.shared.indexing import IndexingStore

from .model import EventRow, JobRow, VersionRow
from .ownership import OwnershipMixin
from .processing import ProcessingMixin
from .queries import QuerySyncMixin
from .recovery import RecoveryMixin
from .registry import RegistryMixin
from .results import ResultsMixin
from .rules import FAILURE_TYPES, LOSS_CODES, RETRYABLE_FAILURES
from .store import InMemoryIndexingStore


class IndexingService(
    RegistryMixin,
    OwnershipMixin,
    ProcessingMixin,
    ResultsMixin,
    RecoveryMixin,
    QuerySyncMixin,
):
    """A small relational ledger; injected I/O runs outside its transaction lock."""

    def __init__(
        self,
        store: IndexingStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], object] = uuid4,
        random_uniform: Callable[[float, float], float] = random.uniform,
        lease_duration: timedelta = timedelta(minutes=5),
        dead_threshold: timedelta = timedelta(seconds=30),
        retry_initial: timedelta = timedelta(seconds=10),
        retry_max: timedelta = timedelta(minutes=5),
        retry_jitter: float = 0.2,
        default_job_max_retries: int = 3,
        default_job_priority: int = 0,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if dead_threshold <= timedelta(0):
            raise ValueError("dead_threshold must be positive")
        if retry_initial <= timedelta(0) or retry_max < retry_initial:
            raise ValueError("invalid retry delay")
        if not 0.0 <= retry_jitter <= 1.0:
            raise ValueError("retry_jitter must be between zero and one")
        if (
            isinstance(default_job_max_retries, bool)
            or not isinstance(default_job_max_retries, int)
            or default_job_max_retries < 0
        ):
            raise ValueError("default_job_max_retries must not be negative")
        if isinstance(default_job_priority, bool) or not isinstance(
            default_job_priority, int
        ):
            raise ValueError("default_job_priority must be an integer")
        self._store = store or InMemoryIndexingStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory
        self._random_uniform = random_uniform
        self.lease_duration = lease_duration
        self.dead_threshold = dead_threshold
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self.retry_jitter = retry_jitter
        self.default_job_max_retries = default_job_max_retries
        self.default_job_priority = default_job_priority
        self._binding_lock = RLock()
        self._document_ledger: DocumentIndexLedger | None = None

    @property
    def state(self):
        """Compatibility view retained for the existing in-memory tests."""

        return self._store.state

    @property
    def _next(self):
        """Compatibility view retained for the existing in-memory tests."""

        return self._store.next_ids

    @property
    def _lock(self):
        """Compatibility view retained for the existing in-memory tests."""

        return self._store.lock

    def _id(self, kind: str, requested: int | None = None) -> int:
        return self._store.next_id(kind, requested)

    def _now(self, supplied: datetime | None = None) -> datetime:
        value = supplied or self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _event_time(self, supplied: datetime | None = None) -> datetime:
        """Domain event time for completion, failure and recovery paths.

        Uses the same precision as claim and attempt-start so that a stored
        ``ended_at`` can never precede its ``started_at`` and ``duration_ms``
        can never be negative. Completion, failure and replay verification
        still compare these values for exact equality.
        """

        return self._now(supplied)

    def _event(self, job_id: int, event_type: str, when: datetime) -> EventRow:
        event = EventRow(self._id("event"), job_id, event_type, when)
        self._store.insert_event(event)
        return event

    @contextmanager
    def document_transaction(self):
        with self._store.transaction():
            yield

    @contextmanager
    def _sync_transaction(self):
        with self.document_transaction():
            yield

    def bind_document_ledger(self, ledger: DocumentIndexLedger) -> None:
        with self._binding_lock:
            if self._document_ledger is not None and self._document_ledger is not ledger:
                raise RuntimeError("indexing service is already bound to a document ledger")
            self._document_ledger = ledger

    def _commit_document_progress(
        self, job: JobRow, version: VersionRow, changed_at: datetime
    ) -> None:
        if self._document_ledger is None:
            return
        document = self._store.get_document(version.document_id)
        if document is None:
            raise RuntimeError("indexing document is missing")
        self._document_ledger.commit_index_progress(
            job_id=job.id,
            version_id=version.id,
            document_id=document.id,
            changed_at=changed_at,
            job_status=job.status,
            version_status=version.status,
            document_status=document.status,
        )
