"""Per-claim lease renewal and cooperative cancellation support."""

from __future__ import annotations

from threading import Event, Thread
from typing import Any

from src.shared import PublicError


class LeaseRenewalMixin:
    """Lease lifecycle shared by the worker runtime.

    The concrete runtime supplies the backend, configuration, locks, clock, and
    logging helpers.  Keeping this concern separate makes the claim scheduler
    readable without changing its public API.
    """

    backend: Any
    config: Any
    _renewals_lock: Any
    _renewals: dict[int, tuple[Event, Event, Thread]]

    def _now(self) -> Any:
        raise NotImplementedError

    def _committed(self) -> None:
        raise NotImplementedError

    def _log_failure(self, classification: str, error: BaseException) -> None:
        raise NotImplementedError

    def _start_renewal(
        self,
        job_id: int,
        worker_id: int,
        claim_token: str,
        interrupt: Event,
    ) -> tuple[Event, Thread]:
        stop = Event()
        thread = Thread(
            target=self._renewal_loop,
            args=(job_id, worker_id, claim_token, stop, interrupt),
            name=f"{self.config.name}-lease-{job_id}",
            daemon=True,
        )
        with self._renewals_lock:
            if job_id in self._renewals:
                raise PublicError("EMBEDDING-JOB-005")
            self._renewals[job_id] = (stop, interrupt, thread)
        try:
            thread.start()
        except Exception:
            with self._renewals_lock:
                self._renewals.pop(job_id, None)
            raise
        return stop, thread

    def _renewal_loop(
        self,
        job_id: int,
        worker_id: int,
        claim_token: str,
        stop: Event,
        interrupt: Event,
    ) -> None:
        authoritative = {
            "WORKER-001",
            "WORKER-002",
            "EMBEDDING-JOB-001",
            "EMBEDDING-JOB-002",
            "EMBEDDING-JOB-003",
            "EMBEDDING-JOB-004",
            "EMBEDDING-JOB-005",
        }
        while not stop.wait(self.config.lease_renew_interval):
            try:
                self.backend.renew(
                    job_id,
                    worker_id,
                    claim_token,
                    now=self._now(),
                )
                self._committed()
            except Exception as error:
                if getattr(error, "code", None) in authoritative:
                    interrupt.set()
                    return
                self._log_failure("WORKER_LEASE_RENEWAL_FAILED", error)

    def _stop_renewal(
        self,
        job_id: int,
        stop: Event,
        interrupt: Event,
        thread: Thread,
    ) -> None:
        stop.set()
        thread.join()
        with self._renewals_lock:
            current = self._renewals.get(job_id)
            if current == (stop, interrupt, thread):
                self._renewals.pop(job_id, None)

    def _cancel_all_renewals(self) -> None:
        with self._renewals_lock:
            renewals = tuple(self._renewals.values())
        for stop, _interrupt, _thread in renewals:
            stop.set()
        for _stop, _interrupt, thread in renewals:
            thread.join()

    def _interrupt_all_claims(self) -> None:
        with self._renewals_lock:
            interrupts = tuple(item[1] for item in self._renewals.values())
        for interrupt in interrupts:
            interrupt.set()
