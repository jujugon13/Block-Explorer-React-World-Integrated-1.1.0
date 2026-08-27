"""Durable outbox state machine, delivery, retry, and reconciliation controls."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import uuid4

from src.shared import PublicError, ReconciliationDetector, SyncEventRecord, SyncUnitOfWork
from src.shared.sync import SyncStateStore

from .consistency import ConsistencyControls, ISSUE_TYPES, SEVERITIES
from .management import ManagementControls
from .model import (
    DeliveryAttemptRow,
    InMemorySyncStore,
    SyncEventRow,
    SyncState,
)
from .outbox import PublicationControls


HANDLER_FAILURE_TYPE = "SYNC_HANDLER_FAILED"
HANDLER_FAILURE_MESSAGE = "동기화 Event 처리 중 오류가 발생했습니다."


def _fail(code: str, message: str | None = None) -> None:
    raise PublicError(code, message)


class SyncService(PublicationControls, ManagementControls, ConsistencyControls):
    """Sync domain rules over an injected state store."""

    def __init__(
        self,
        store: SyncStateStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], object] = uuid4,
        detector: ReconciliationDetector | None = None,
        dispatcher_enabled: bool = False,
        dispatcher_name: str = "sync-dispatcher",
        poll_interval: timedelta = timedelta(seconds=1),
        lease_duration: timedelta = timedelta(seconds=30),
        recovery_interval: timedelta = timedelta(seconds=10),
        recovery_batch: int = 100,
        retry_initial: timedelta = timedelta(seconds=5),
        retry_max: timedelta = timedelta(minutes=1),
        default_max_retries: int = 5,
        reconciliation_enabled: bool = False,
        reconciliation_mode: str = "DRY_RUN",
        reconciliation_batch: int = 100,
        reconciliation_interval: timedelta = timedelta(minutes=5),
        stalled_threshold: timedelta = timedelta(minutes=15),
    ) -> None:
        if (
            poll_interval <= timedelta(0)
            or lease_duration <= timedelta(0)
            or recovery_interval <= timedelta(0)
        ):
            raise ValueError("sync periods must be positive")
        if recovery_batch < 1:
            raise ValueError("recovery_batch must be positive")
        if retry_initial <= timedelta(0) or retry_max < retry_initial:
            raise ValueError("invalid retry delay")
        if default_max_retries < 0:
            raise ValueError("default_max_retries must not be negative")
        if reconciliation_batch < 1:
            raise ValueError("reconciliation_batch must be positive")
        if reconciliation_mode not in {"DRY_RUN", "REPAIR"}:
            raise ValueError("invalid reconciliation mode")
        if (
            reconciliation_interval <= timedelta(0)
            or stalled_threshold <= timedelta(0)
        ):
            raise ValueError("reconciliation periods must be positive")
        self.store = store if store is not None else InMemorySyncStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory
        self._detector = detector
        self.dispatcher_enabled = dispatcher_enabled
        self.dispatcher_name = dispatcher_name
        self.poll_interval = poll_interval
        self.lease_duration = lease_duration
        self.recovery_interval = recovery_interval
        self.recovery_batch = recovery_batch
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self.default_max_retries = default_max_retries
        self.reconciliation_enabled = reconciliation_enabled
        self.reconciliation_mode = reconciliation_mode
        self.reconciliation_batch = reconciliation_batch
        self.reconciliation_interval = reconciliation_interval
        self.stalled_threshold = stalled_threshold
        self._poll_lock = Lock()

    _fail = staticmethod(_fail)

    @property
    def state(self) -> SyncState:
        """Expose the live default state for backward-compatible tests only."""

        state = getattr(self.store, "state", None)
        if not isinstance(state, SyncState):
            raise AttributeError("the configured sync store has no in-memory state")
        return state

    def _id(self) -> str:
        return str(self._uuid_factory())

    def _now(self, supplied: datetime | None = None) -> datetime:
        value = supplied or self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _finished_at(self, started_at: datetime) -> datetime:
        """Use the live clock without moving behind an explicitly supplied start."""

        return max(started_at, self._now())

    @contextmanager
    def transaction(self):
        """Enlist sync outbox writes in the configured store transaction."""

        with self.store.transaction():
            yield self

    def claim(self, owner_name: str, now: datetime | None = None) -> SyncEventRow | None:
        moment = self._now(now)
        candidates = sorted(
            (
                event
                for event in self.store.list_events()
                if event.status == "PENDING"
                and event.available_at is not None
                and event.available_at <= moment
            ),
            key=lambda event: (event.available_at, event.occurred_at, event.id),
        )
        for candidate in candidates:
            with self.store.transaction():
                event = self.store.get_event(
                    candidate.id, for_update=True, skip_locked=True
                )
                if (
                    event is None
                    or event.status != "PENDING"
                    or event.available_at is None
                    or event.available_at > moment
                ):
                    continue
                event.status = "PROCESSING"
                event.owner_name = owner_name
                event.claim_token = self._id()
                event.locked_at = moment
                event.lease_expires_at = moment + self.lease_duration
                attempts = self.store.list_attempts(event.id)
                attempt_no = 1 + sum(row.event_id == event.id for row in attempts)
                self.store.save_event(event)
                self.store.insert_attempt(
                    DeliveryAttemptRow(
                        self._id(), event.id, attempt_no, "STARTED", moment
                    )
                )
                return event
        return None

    def _owned(
        self,
        event: SyncEventRow,
        owner_name: str,
        claim_token: str,
        now: datetime,
    ) -> None:
        if (
            event.status != "PROCESSING"
            or event.owner_name != owner_name
            or event.claim_token != claim_token
            or event.lease_expires_at is None
            or event.lease_expires_at <= now
        ):
            _fail("SYNC-002")

    def _attempt(self, event_id: str) -> DeliveryAttemptRow:
        for attempt in reversed(self.store.list_attempts(event_id)):
            if attempt.event_id == event_id and attempt.status == "STARTED":
                return attempt
        _fail("SYNC-003")

    @staticmethod
    def _clear_owner(event: SyncEventRow) -> None:
        event.owner_name = None
        event.claim_token = None
        event.locked_at = None
        event.lease_expires_at = None

    def _complete(
        self,
        event_id: str,
        owner_name: str,
        claim_token: str,
        now: datetime,
    ) -> None:
        with self.store.transaction():
            event = self.store.get_event(event_id, for_update=True)
            if event is None:
                _fail("SYNC-001")
            self._owned(event, owner_name, claim_token, now)
            attempt = self._attempt(event.id)
            event.status = "PROCESSED"
            event.processed_at = now
            event.failed_at = None
            event.error_type = None
            event.error_message = None
            self._clear_owner(event)
            attempt.status = "SUCCEEDED"
            attempt.ended_at = now
            self.store.save_event(event)
            self.store.save_attempt(attempt)

    def dispatch_one(
        self,
        owner_name: str,
        unit_of_work: SyncUnitOfWork,
        now: datetime | None = None,
    ) -> SyncEventRow | None:
        if not self._poll_lock.acquire(blocking=False):
            return None
        try:
            moment = self._now(now)
            event = self.claim(owner_name, moment)
            if event is None:
                return None
            token = event.claim_token
            if token is None:
                _fail("SYNC-003")
            record = SyncEventRecord(
                event.id,
                event.aggregate_type,
                event.aggregate_id,
                event.aggregate_version,
                event.event_type,
                deepcopy(event.payload),
                event.occurred_at,
            )
            try:
                with self.store.transaction():
                    unit_of_work.commit(
                        record,
                        lambda: self._complete(
                            event.id,
                            owner_name,
                            token,
                            self._finished_at(moment),
                        ),
                    )
                    current = self.store.get_event(event.id, for_update=True)
                    if current is None or current.status != "PROCESSED":
                        raise RuntimeError("unit of work did not complete the event")
            except Exception:
                current = self.store.get_event(event.id)
                if current is not None and current.status not in {
                    "PROCESSING",
                    "PROCESSED",
                }:
                    return current
                try:
                    self.record_failure(
                        event.id,
                        owner_name,
                        token,
                        HANDLER_FAILURE_TYPE,
                        HANDLER_FAILURE_MESSAGE,
                        self._finished_at(moment),
                    )
                except PublicError as ownership_error:
                    if ownership_error.code != "SYNC-002":
                        raise
            return self.event(event.id)
        finally:
            self._poll_lock.release()

    def _retry_delay(self, failure_count: int) -> timedelta:
        delay = self.retry_initial
        for _ in range(max(0, failure_count - 1)):
            if delay >= self.retry_max / 2:
                return self.retry_max
            delay *= 2
        return min(delay, self.retry_max)

    def _record_failure(
        self,
        event: SyncEventRow,
        error_type: str,
        error_message: str,
        moment: datetime,
    ) -> None:
        attempt = self._attempt(event.id)
        event.failure_count += 1
        event.failed_at = moment
        event.error_type = error_type
        event.error_message = error_message
        event.processed_at = None
        event.status = (
            "PENDING" if event.failure_count <= event.max_retries else "FAILED"
        )
        if event.status == "PENDING":
            event.available_at = moment + self._retry_delay(event.failure_count)
        else:
            event.available_at = None
        self._clear_owner(event)
        attempt.status = "FAILED"
        attempt.ended_at = moment
        attempt.error_type = error_type
        attempt.error_message = error_message
        self.store.save_event(event)
        self.store.save_attempt(attempt)

    def record_failure(
        self,
        event_id: str,
        owner_name: str,
        claim_token: str,
        error_type: str,
        error_message: str,
        now: datetime | None = None,
    ) -> SyncEventRow:
        del error_type, error_message
        moment = self._now(now)
        with self.store.transaction():
            event = self.store.get_event(event_id, for_update=True)
            if event is None:
                _fail("SYNC-001")
            self._owned(event, owner_name, claim_token, moment)
            self._record_failure(
                event,
                HANDLER_FAILURE_TYPE,
                HANDLER_FAILURE_MESSAGE,
                moment,
            )
            return event

    def recover_expired(
        self,
        now: datetime | None = None,
        *,
        batch_size: int | None = None,
    ) -> tuple[SyncEventRow, ...]:
        cutoff = self._now(now).replace(microsecond=0)
        limit = self.recovery_batch if batch_size is None else batch_size
        if limit < 1:
            raise ValueError("batch_size must be positive")
        candidate_ids = tuple(
            event.id
            for event in sorted(
                (
                    event
                    for event in self.store.list_events()
                    if event.status == "PROCESSING"
                    and event.lease_expires_at is not None
                    and event.lease_expires_at <= cutoff
                ),
                key=lambda event: (event.lease_expires_at, event.id),
            )[:limit]
        )
        recovered: list[SyncEventRow] = []
        for event_id in candidate_ids:
            with self.store.transaction():
                event = self.store.get_event(
                    event_id, for_update=True, skip_locked=True
                )
                if (
                    event is None
                    or event.status != "PROCESSING"
                    or event.lease_expires_at is None
                    or event.lease_expires_at > cutoff
                ):
                    continue
                self._record_failure(
                    event,
                    "LEASE_EXPIRED",
                    "동기화 Event Lease가 만료되었습니다.",
                    cutoff,
                )
                recovered.append(event)
        return tuple(recovered)
