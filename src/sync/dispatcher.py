"""Fixed-delay sync polling, expired-lease recovery, and reconciliation ticks."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from time import monotonic

from src.shared import Identifier, SyncUnitOfWork

from .core import SyncService
from .model import ReconciliationRunRow, SyncEventRow


class SyncDispatcher:
    """Run one event per poll and honor the two sync role toggles."""

    def __init__(
        self,
        service: SyncService,
        unit_of_work: SyncUnitOfWork,
        *,
        system_actor_id: Identifier = "sync-dispatcher",
        monotonic_clock: Callable[[], float] = monotonic,
        state_transition_committed: Callable[[], None] | None = None,
    ) -> None:
        self.service = service
        self.unit_of_work = unit_of_work
        self.system_actor_id = system_actor_id
        self._monotonic = monotonic_clock
        self._state_transition_committed = state_transition_committed

    def bind_state_transition_listener(self, listener: Callable[[], None]) -> None:
        self._state_transition_committed = listener

    def _committed(self, result: object) -> object:
        if result is not None and result != () and self._state_transition_committed is not None:
            self._state_transition_committed()
        return result

    def tick(self) -> SyncEventRow | None:
        if not self.service.dispatcher_enabled:
            return None
        return self._committed(
            self.service.dispatch_one(
                self.service.dispatcher_name,
                self.unit_of_work,
            )
        )

    def recovery_tick(self) -> tuple[SyncEventRow, ...]:
        if not self.service.dispatcher_enabled:
            return ()
        return self._committed(self.service.recover_expired())  # type: ignore[return-value]

    def reconciliation_tick(self) -> ReconciliationRunRow | None:
        if not self.service.reconciliation_enabled:
            return None
        return self._committed(
            self.service.reconcile(
                mode=self.service.reconciliation_mode,
                actor_id=self.system_actor_id,
            )
        )

    def run(self, stop: Event) -> None:
        """Run fixed-delay work; the application owns the scheduler thread."""

        if not (
            self.service.dispatcher_enabled
            or self.service.reconciliation_enabled
        ):
            return
        recovery_due = self._monotonic() + self.service.recovery_interval.total_seconds()
        reconciliation_due = (
            self._monotonic() + self.service.reconciliation_interval.total_seconds()
        )
        while not stop.wait(self.service.poll_interval.total_seconds()):
            self.tick()
            current = self._monotonic()
            if current >= recovery_due:
                self.recovery_tick()
                recovery_due = self._monotonic() + self.service.recovery_interval.total_seconds()
            if current >= reconciliation_due:
                self.reconciliation_tick()
                reconciliation_due = (
                    self._monotonic()
                    + self.service.reconciliation_interval.total_seconds()
                )
