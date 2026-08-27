"""Indexing worker registration, polling, recovery, and shutdown lifecycle."""

from __future__ import annotations

import logging
import socket
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import BoundedSemaphore, Event, Lock, RLock, Thread
from typing import Any

from src.shared import PublicError
from src.worker.renewal import LeaseRenewalMixin


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    enabled: bool = False
    name: str = "indexing-worker"
    heartbeat_interval: float = 10.0
    dead_threshold: float = 30.0
    poll_interval: float = 1.0
    empty_poll_max: float = 10.0
    max_concurrency: int = 2
    lease_duration: float = 300.0
    lease_renew_interval: float = 60.0
    recovery_interval: float = 30.0
    recovery_batch_size: int = 100
    shutdown_grace: float = 30.0

    def __post_init__(self) -> None:
        if self.heartbeat_interval <= 0 or self.heartbeat_interval >= self.dead_threshold:
            raise ValueError("heartbeat interval must be positive and below dead threshold")
        if self.poll_interval <= 0 or self.poll_interval > self.empty_poll_max:
            raise ValueError("poll interval must be positive and at most empty-poll maximum")
        if self.max_concurrency <= 0:
            raise ValueError("max concurrency must be positive")
        if self.lease_duration <= 0:
            raise ValueError("lease duration must be positive")
        if self.lease_renew_interval <= 0 or self.lease_renew_interval >= self.lease_duration:
            raise ValueError("lease renewal must be positive and below lease duration")
        if self.recovery_interval <= 0 or self.recovery_batch_size <= 0:
            raise ValueError("lease recovery settings must be positive")
        if self.shutdown_grace < 0:
            raise ValueError("shutdown grace cannot be negative")


def effective_status(
    stored_status: str,
    last_heartbeat: datetime,
    now: datetime,
    dead_threshold: float | timedelta = 30.0,
) -> str:
    """Return the externally visible worker status; equality is already dead."""

    if stored_status in {"STOPPED", "DEAD"}:
        return stored_status
    threshold = (
        dead_threshold
        if isinstance(dead_threshold, timedelta)
        else timedelta(seconds=dead_threshold)
    )
    return stored_status if last_heartbeat > now - threshold else "DEAD"


def _host_info() -> tuple[str, str | None]:
    try:
        hostname = socket.gethostname()
    except OSError:
        return "unknown", None
    try:
        return hostname, socket.gethostbyname(hostname)
    except OSError:
        return hostname or "unknown", None


class WorkerRuntime(LeaseRenewalMixin):
    """Small runtime around an injected indexing backend and claim executor."""

    def __init__(
        self,
        backend: Any,
        execute_claim: Callable[[Mapping[str, object], Event], None],
        config: WorkerConfig = WorkerConfig(),
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        uuid_factory: Callable[[], object] | None = None,
        host_info: Callable[[], tuple[str, str | None]] | None = None,
        logger: logging.Logger | None = None,
        state_transition_committed: Callable[[], None] | None = None,
    ) -> None:
        self.backend = backend
        self.execute_claim = execute_claim
        self.config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._uuid_factory = uuid_factory or uuid.uuid4
        self._host_info = host_info or _host_info
        self._log = logger or logging.getLogger(__name__)
        self._state_transition_committed = state_transition_committed

        self._lifecycle = RLock()
        self._poll_guard = Lock()
        self._futures_lock = Lock()
        self._renewals_lock = Lock()
        self._scheduler_stop = Event()
        self._interrupt = Event()
        self._slots = BoundedSemaphore(config.max_concurrency)
        self._executor: ThreadPoolExecutor | None = None
        self._futures: set[Future[None]] = set()
        self._renewals: dict[int, tuple[Event, Event, Thread]] = {}
        self._scheduler_threads: list[Thread] = []
        self._worker_id: int | None = None
        self._instance_id: str | None = None
        self._accepting = False
        self._closed = False
        self._claim_delay = config.poll_interval
        self._next_claim_at = 0.0

    def bind_state_transition_listener(self, listener: Callable[[], None]) -> None:
        self._state_transition_committed = listener

    def _committed(self) -> None:
        if self._state_transition_committed is not None:
            self._state_transition_committed()

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _log_failure(self, classification: str, error: BaseException) -> None:
        self._log.error(
            "%s error_type=%s",
            classification,
            type(error).__name__,
        )

    @staticmethod
    def _row_id(row: object) -> int:
        if isinstance(row, Mapping):
            value = row.get("workerId", row.get("worker_id", row.get("id")))
        else:
            value = getattr(row, "worker_id", getattr(row, "id", None))
        if not isinstance(value, int):
            raise TypeError("registered worker row has no integer identifier")
        return value

    @staticmethod
    def _row_status(row: object) -> str | None:
        if isinstance(row, Mapping):
            value = row.get("status")
        else:
            value = getattr(row, "status", None)
        return value if isinstance(value, str) else None

    @property
    def worker_id(self) -> int | None:
        return self._worker_id

    @property
    def instance_id(self) -> str | None:
        return self._instance_id

    @property
    def claim_delay(self) -> float:
        return self._claim_delay

    @property
    def interrupt_requested(self) -> bool:
        return self._interrupt.is_set()

    def start(self, *, background: bool = True) -> object | None:
        """Register one new process instance and optionally start fixed-delay loops."""

        with self._lifecycle:
            if self._closed:
                raise RuntimeError("worker runtime is closed")
            if self._worker_id is not None or not self.config.enabled:
                return None
            instance_id = str(self._uuid_factory())
            try:
                hostname, ip_address = self._host_info()
            except Exception:
                hostname, ip_address = "unknown", None
            try:
                row = self.backend.register_worker(
                    instance_id,
                    name=self.config.name,
                    hostname=hostname or "unknown",
                    ip=ip_address,
                    now=self._now(),
                )
                worker_id = self._row_id(row)
            except Exception as error:
                self._log_failure("WORKER_REGISTRATION_FAILED", error)
                return None

            self._instance_id = instance_id
            self._worker_id = worker_id
            self._committed()
            if self._row_status(row) == "STOPPED":
                return row
            self._accepting = True
            self._executor = ThreadPoolExecutor(
                max_workers=self.config.max_concurrency,
                thread_name_prefix=self.config.name,
            )
            if background:
                self._start_schedulers()
            return row

    def _start_schedulers(self) -> None:
        schedules = (
            ("heartbeat", self.config.heartbeat_interval, self.heartbeat_tick),
            ("poll", self.config.poll_interval, self.poll_tick),
            ("recovery", self.config.recovery_interval, self.recovery_tick),
        )
        for label, delay, action in schedules:
            thread = Thread(
                target=self._fixed_delay_loop,
                args=(delay, action),
                name=f"{self.config.name}-{label}",
                daemon=True,
            )
            self._scheduler_threads.append(thread)
            thread.start()

    def _fixed_delay_loop(self, delay: float, action: Callable[[], object]) -> None:
        if self._scheduler_stop.wait(delay):
            return
        while not self._scheduler_stop.is_set():
            try:
                action()
            except Exception as error:
                self._log_failure("WORKER_SCHEDULED_ACTION_FAILED", error)
            if self._scheduler_stop.wait(delay):
                return

    def heartbeat_tick(self) -> bool:
        worker_id = self._worker_id
        if worker_id is None or not self._accepting:
            return False
        try:
            self.backend.heartbeat(worker_id, now=self._now())
            self._committed()
            return True
        except Exception as error:
            self._log_failure("WORKER_HEARTBEAT_FAILED", error)
            return False

    @staticmethod
    def _claim_result(result: object) -> tuple[int, Mapping[str, object] | None]:
        if result is None:
            return 204, None
        status = getattr(result, "status", None)
        data = getattr(result, "data", None)
        if not isinstance(status, int):
            raise TypeError("claim result has no HTTP status")
        if status == 204:
            return status, None
        if status != 200 or not isinstance(data, Mapping):
            raise ValueError(f"unexpected claim response: {status}")
        return status, data

    def poll_tick(self) -> int:
        """Fill free local slots; return how many claims were submitted."""

        worker_id = self._worker_id
        if worker_id is None or not self._accepting:
            return 0
        tick = self._monotonic()
        if tick < self._next_claim_at or not self._poll_guard.acquire(blocking=False):
            return 0
        submitted = 0
        try:
            for _ in range(self.config.max_concurrency):
                if not self._accepting or not self._slots.acquire(blocking=False):
                    break
                if not self._accepting:
                    self._slots.release()
                    break
                try:
                    result = self.backend.claim(worker_id, now=self._now())
                    status, claim = self._claim_result(result)
                except Exception as error:
                    self._slots.release()
                    self._next_claim_at = tick + self._claim_delay
                    self._log_failure("WORKER_CLAIM_FAILED", error)
                    break

                if status == 204:
                    self._slots.release()
                    self._claim_delay = min(
                        self.config.empty_poll_max,
                        max(self.config.poll_interval, self._claim_delay * 2),
                    )
                    self._next_claim_at = tick + self._claim_delay
                    break

                self._committed()

                self._claim_delay = self.config.poll_interval
                self._next_claim_at = tick + self.config.poll_interval
                executor = self._executor
                try:
                    if executor is None:
                        raise RuntimeError("worker executor is unavailable")
                    future = executor.submit(self._run_claim, claim)
                except RuntimeError:
                    self._slots.release()
                    break
                with self._futures_lock:
                    self._futures.add(future)
                future.add_done_callback(self._forget_future)
                submitted += 1
            return submitted
        finally:
            self._poll_guard.release()

    @staticmethod
    def _claim_identity(claim: Mapping[str, object]) -> tuple[int, int, str]:
        job_id = claim.get("jobId")
        worker_id = claim.get("workerId")
        version_id = claim.get("documentVersionId")
        token = claim.get("claimToken")
        if (
            claim.get("status") != "PROCESSING"
            or isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id <= 0
            or isinstance(worker_id, bool)
            or not isinstance(worker_id, int)
            or worker_id <= 0
            or isinstance(version_id, bool)
            or not isinstance(version_id, int)
            or version_id <= 0
            or not isinstance(token, str)
            or not token
        ):
            raise PublicError("EMBEDDING-JOB-005")
        return job_id, worker_id, token

    def _run_claim(self, claim: Mapping[str, object] | None) -> None:
        renewal: tuple[int, Event, Event, Thread] | None = None
        try:
            if claim is None:
                raise ValueError("successful claim has no data")
            job_id, worker_id, claim_token = self._claim_identity(claim)
            interrupt = Event()
            stop, thread = self._start_renewal(
                job_id,
                worker_id,
                claim_token,
                interrupt,
            )
            renewal = (job_id, stop, interrupt, thread)
            self.execute_claim(claim, interrupt)
            self._committed()
        finally:
            if renewal is not None:
                self._stop_renewal(*renewal)
            self._slots.release()

    def _forget_future(self, future: Future[None]) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        try:
            error = future.exception()
        except CancelledError:
            return
        if error is not None:
            self._log_failure("WORKER_EXECUTION_FAILED", error)

    def recovery_tick(self) -> object | None:
        if self._worker_id is None or not self._accepting:
            return None
        now = self._now()
        try:
            result = self.backend.recover_expired(
                now=now,
                batch_size=self.config.recovery_batch_size,
            )
            self._committed()
            return result
        except Exception as error:
            self._log_failure("WORKER_RECOVERY_FAILED", error)
            return None

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        with self._futures_lock:
            pending = tuple(self._futures)
        if not pending:
            return True
        _, unfinished = wait(pending, timeout=timeout)
        return not unfinished

    def shutdown(self) -> None:
        """Stop acquisition, drain work, then record STOPPED as the last action."""

        with self._lifecycle:
            if self._closed:
                return
            self._accepting = False
            self._scheduler_stop.set()
            executor = self._executor
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=False)

            for thread in tuple(self._scheduler_threads):
                thread.join(timeout=0.1)

            with self._futures_lock:
                pending = tuple(self._futures)
            if pending:
                _, unfinished = wait(pending, timeout=self.config.shutdown_grace)
                if unfinished:
                    # ponytail: Python cannot kill threads; callbacks cooperatively
                    # observe this event and leases remain for normal recovery.
                    self._interrupt.set()
                    self._interrupt_all_claims()
                    for future in unfinished:
                        future.cancel()

            self._cancel_all_renewals()

            worker_id = self._worker_id
            if worker_id is not None:
                try:
                    self.backend.stop_worker(worker_id, now=self._now())
                    self._committed()
                except Exception as error:
                    self._log_failure("WORKER_STOP_RECORD_FAILED", error)
            self._closed = True
