"""Sync ledger rows and the default in-memory state store."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock

from src.shared.sync import (
    ConsistencyIssueRow,
    ReconciliationRunRow,
    SyncDeliveryAttemptRow,
    SyncEventRow,
    SyncOperatorActionRow,
)


# Preserve the public names exported by src.sync.
DeliveryAttemptRow = SyncDeliveryAttemptRow
OperatorActionRow = SyncOperatorActionRow


@dataclass(slots=True)
class SyncState:
    events: dict[str, SyncEventRow] = field(default_factory=dict)
    event_id_by_key: dict[str, str] = field(default_factory=dict)
    attempts: list[DeliveryAttemptRow] = field(default_factory=list)
    issues: dict[str, ConsistencyIssueRow] = field(default_factory=dict)
    actions: list[OperatorActionRow] = field(default_factory=list)
    reconciliations: dict[str, ReconciliationRunRow] = field(default_factory=dict)


class InMemorySyncStore:
    """Thread-safe default store preserving the original live-row behavior."""

    def __init__(self) -> None:
        self.state = SyncState()
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            before = deepcopy(self.state)
            try:
                yield
            except Exception:
                self.state = before
                raise

    def insert_event(self, event: SyncEventRow) -> bool:
        with self._lock:
            if event.idempotency_key in self.state.event_id_by_key:
                return False
            if event.id in self.state.events:
                raise ValueError("duplicate sync event ID")
            self.state.events[event.id] = event
            self.state.event_id_by_key[event.idempotency_key] = event.id
            return True

    def get_event(
        self,
        event_id: str,
        *,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> SyncEventRow | None:
        del for_update, skip_locked
        with self._lock:
            return self.state.events.get(event_id)

    def get_event_by_key(
        self, idempotency_key: str, *, for_update: bool = False
    ) -> SyncEventRow | None:
        del for_update
        with self._lock:
            event_id = self.state.event_id_by_key.get(idempotency_key)
            return self.state.events.get(event_id) if event_id is not None else None

    def list_events(self) -> tuple[SyncEventRow, ...]:
        with self._lock:
            return tuple(self.state.events.values())

    def save_event(self, event: SyncEventRow) -> None:
        with self._lock:
            if event.id not in self.state.events:
                raise KeyError(event.id)
            self.state.events[event.id] = event

    def insert_attempt(self, attempt: DeliveryAttemptRow) -> None:
        with self._lock:
            if any(row.id == attempt.id for row in self.state.attempts):
                raise ValueError("duplicate sync delivery attempt ID")
            self.state.attempts.append(attempt)

    def list_attempts(self, event_id: str) -> tuple[DeliveryAttemptRow, ...]:
        with self._lock:
            return tuple(
                row for row in self.state.attempts if row.event_id == event_id
            )

    def save_attempt(self, attempt: DeliveryAttemptRow) -> None:
        with self._lock:
            for index, row in enumerate(self.state.attempts):
                if row.id == attempt.id:
                    self.state.attempts[index] = attempt
                    return
            raise KeyError(attempt.id)

    def insert_issue(self, issue: ConsistencyIssueRow) -> None:
        with self._lock:
            if issue.id in self.state.issues:
                raise ValueError("duplicate consistency issue ID")
            self.state.issues[issue.id] = issue

    def get_issue(
        self, issue_id: str, *, for_update: bool = False
    ) -> ConsistencyIssueRow | None:
        del for_update
        with self._lock:
            return self.state.issues.get(issue_id)

    def list_issues(self) -> tuple[ConsistencyIssueRow, ...]:
        with self._lock:
            return tuple(self.state.issues.values())

    def save_issue(self, issue: ConsistencyIssueRow) -> None:
        with self._lock:
            if issue.id not in self.state.issues:
                raise KeyError(issue.id)
            self.state.issues[issue.id] = issue

    def insert_action(self, action: OperatorActionRow) -> None:
        with self._lock:
            if any(row.id == action.id for row in self.state.actions):
                raise ValueError("duplicate operator action ID")
            self.state.actions.append(action)

    def insert_reconciliation(self, run: ReconciliationRunRow) -> None:
        with self._lock:
            if run.id in self.state.reconciliations:
                raise ValueError("duplicate reconciliation ID")
            self.state.reconciliations[run.id] = run

    def save_reconciliation(self, run: ReconciliationRunRow) -> None:
        with self._lock:
            if run.id not in self.state.reconciliations:
                raise KeyError(run.id)
            self.state.reconciliations[run.id] = run
