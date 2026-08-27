from __future__ import annotations

import logging
import unittest
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread

from src.shared import PublicError
from src.worker import WorkerConfig, WorkerRuntime, effective_status


NOW = datetime(2026, 8, 27, tzinfo=UTC)


@dataclass
class Row:
    id: int
    instance_id: str
    name: str
    hostname: str
    ip: str | None
    status: str
    last_heartbeat: datetime


@dataclass(frozen=True)
class Result:
    status: int
    data: dict[str, object] | None = None


class MutableClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class Monotonic:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.items: list[logging.LogRecord] = []

    def emit(self, record):
        self.items.append(record)


class Backend:
    def __init__(self):
        self.rows: list[Row] = []
        self.claims: deque[Result] = deque()
        self.claim_error: Exception | None = None
        self.claim_calls = 0
        self.recovery_calls: list[tuple[datetime, int]] = []
        self.renewals: list[tuple[int, int, str, datetime]] = []
        self.renew_error: Exception | None = None
        self.renew_errors: dict[int, Exception] = {}
        self.renewed = Event()
        self.jobs: list[dict[str, object]] = []
        self.operations: list[str] = []
        self.claim_entered: Event | None = None
        self.claim_release: Event | None = None
        self._lock = Lock()

    def register_worker(self, instance_id, *, name, hostname, ip, now):
        with self._lock:
            duplicate = any(row.instance_id == instance_id for row in self.rows)
            row = Row(
                len(self.rows) + 1,
                instance_id,
                name,
                hostname,
                ip,
                "STOPPED" if duplicate else "ACTIVE",
                now,
            )
            self.rows.append(row)
            self.operations.append("register")
            return row

    def heartbeat(self, worker_id, *, now):
        row = self.rows[worker_id - 1]
        row.last_heartbeat = now
        self.operations.append("heartbeat")

    def stop_worker(self, worker_id, *, now):
        self.rows[worker_id - 1].status = "STOPPED"
        self.operations.append("stop")

    def claim(self, worker_id, *, now):
        with self._lock:
            self.claim_calls += 1
        if self.claim_entered is not None:
            self.claim_entered.set()
        if self.claim_release is not None:
            self.claim_release.wait(1)
        if self.claim_error is not None:
            raise self.claim_error
        return self.claims.popleft() if self.claims else Result(204)

    def recover_expired(self, *, now, batch_size):
        self.recovery_calls.append((now, batch_size))
        candidates = sorted(
            (
                job
                for job in self.jobs
                if job["status"] == "PROCESSING" and job["expires_at"] <= now
            ),
            key=lambda job: (job["expires_at"], job["id"]),
        )[:batch_size]
        for job in candidates:
            job["retry_count"] += 1
            job["status"] = (
                "PENDING"
                if job["retry_count"] <= job["max_retries"]
                else "FAILED"
            )
        return {"candidateCount": len(candidates), "recoveredCount": len(candidates)}

    def renew(self, job_id, worker_id, claim_token, *, now):
        renew_error = self.renew_errors.get(job_id, self.renew_error)
        if renew_error is not None:
            self.renewed.set()
            raise renew_error
        with self._lock:
            self.renewals.append((job_id, worker_id, claim_token, now))
        self.renewed.set()
        return Result(200, {"jobId": job_id, "leaseExpiresAt": now})


def claim(job_id=1):
    return {
        "jobId": job_id,
        "workerId": 1,
        "documentVersionId": 1,
        "status": "PROCESSING",
        "claimToken": "token",
        "lockedAt": NOW,
        "leaseExpiresAt": NOW + timedelta(minutes=5),
    }


class WorkerAcceptanceTests(unittest.TestCase):
    def runtime(
        self,
        backend,
        execute=lambda _claim, _cancel: None,
        *,
        config=None,
        clock=None,
        monotonic=None,
        uuid_factory=None,
        logger=None,
    ):
        return WorkerRuntime(
            backend,
            execute,
            config or WorkerConfig(enabled=True),
            clock=clock or (lambda: NOW),
            monotonic=monotonic,
            uuid_factory=uuid_factory,
            host_info=lambda: ("host", "127.0.0.1"),
            logger=logger,
        )

    def test_AC_IDX_050_disabled_default_and_each_process_gets_a_new_row(self):
        backend = Backend()
        disabled = WorkerRuntime(backend, lambda _claim, _cancel: None)
        self.assertIsNone(disabled.start(background=False))
        self.assertEqual([], backend.rows)
        disabled.shutdown()

        values = iter(("instance-one", "instance-two"))
        first = self.runtime(backend, uuid_factory=lambda: next(values))
        first.start(background=False)
        first.shutdown()
        second = self.runtime(backend, uuid_factory=lambda: next(values))
        second.start(background=False)

        self.assertEqual(2, len(backend.rows))
        self.assertEqual(["instance-one", "instance-two"], [r.instance_id for r in backend.rows])
        self.assertEqual("ACTIVE", backend.rows[1].status)
        second.shutdown()

    def test_AC_IDX_050_later_duplicate_registration_stays_stopped(self):
        backend = Backend()
        first = self.runtime(backend, uuid_factory=lambda: "same-instance")
        second = self.runtime(backend, uuid_factory=lambda: "same-instance")
        first.start(background=False)
        second.start(background=False)

        self.assertEqual("STOPPED", backend.rows[1].status)
        self.assertFalse(second.heartbeat_tick())
        self.assertEqual(0, second.poll_tick())
        first.shutdown()
        second.shutdown()

    def test_AC_IDX_051_normal_shutdown_records_stopped_last(self):
        backend = Backend()
        runtime = self.runtime(backend)
        runtime.start(background=False)
        runtime.shutdown()

        self.assertEqual("STOPPED", backend.rows[0].status)
        self.assertEqual("stop", backend.operations[-1])

    def test_AC_IDX_052_heartbeat_boundary_is_dead_and_terminal_states_stay_terminal(self):
        clock = MutableClock()
        backend = Backend()
        runtime = self.runtime(backend, clock=clock)
        runtime.start(background=False)

        clock.value = NOW + timedelta(seconds=5)
        self.assertTrue(runtime.heartbeat_tick())
        last = backend.rows[0].last_heartbeat
        self.assertEqual("ACTIVE", effective_status("ACTIVE", last, clock.value + timedelta(seconds=29)))
        self.assertEqual("DEAD", effective_status("ACTIVE", last, clock.value + timedelta(seconds=30)))
        self.assertEqual("STOPPED", effective_status("STOPPED", NOW, clock.value + timedelta(days=1)))
        runtime.shutdown()

    def test_AC_IDX_053_recovery_tick_routes_expired_jobs_by_remaining_retries(self):
        backend = Backend()
        backend.jobs = [
            {
                "id": 2,
                "status": "PROCESSING",
                "expires_at": NOW,
                "retry_count": 2,
                "max_retries": 3,
            },
            {
                "id": 1,
                "status": "PROCESSING",
                "expires_at": NOW - timedelta(seconds=1),
                "retry_count": 3,
                "max_retries": 3,
            },
        ]
        runtime = self.runtime(backend)
        runtime.start(background=False)
        result = runtime.recovery_tick()

        self.assertEqual({1: "FAILED", 2: "PENDING"}, {j["id"]: j["status"] for j in backend.jobs})
        self.assertEqual([(NOW, 100)], backend.recovery_calls)
        self.assertEqual(2, result["recoveredCount"])
        runtime.shutdown()

    def test_AC_IDX_054_empty_queue_backoff_is_1_2_4_8_10_and_stays_capped(self):
        backend = Backend()
        monotonic = Monotonic()
        runtime = self.runtime(backend, monotonic=monotonic)
        runtime.start(background=False)

        observed = [runtime.claim_delay]
        for _ in range(5):
            runtime.poll_tick()
            observed.append(runtime.claim_delay)
            monotonic.value += runtime.claim_delay

        self.assertEqual([1.0, 2.0, 4.0, 8.0, 10.0, 10.0], observed)
        self.assertEqual(5, backend.claim_calls)
        runtime.shutdown()

    def test_AC_IDX_055_claim_errors_do_not_increase_backoff(self):
        backend = Backend()
        backend.claim_error = RuntimeError("secret database connection details")
        monotonic = Monotonic()
        records = Records()
        logger = logging.Logger("worker-test")
        logger.addHandler(records)
        runtime = self.runtime(backend, monotonic=monotonic, logger=logger)
        runtime.start(background=False)

        for _ in range(4):
            runtime.poll_tick()
            self.assertEqual(1.0, runtime.claim_delay)
            monotonic.value += 1

        self.assertEqual(4, backend.claim_calls)
        self.assertTrue(all("RuntimeError" in item.getMessage() for item in records.items))
        self.assertTrue(all("secret" not in item.getMessage() for item in records.items))
        self.assertTrue(all(item.exc_info is None for item in records.items))
        runtime.shutdown()

    def test_AC_IDX_056_full_slot_prevents_claim_before_database_access(self):
        backend = Backend()
        backend.claims.append(Result(200, claim()))
        monotonic = Monotonic()
        started, release = Event(), Event()

        def execute(_claim, _cancel):
            started.set()
            release.wait(1)

        runtime = self.runtime(
            backend,
            execute,
            config=WorkerConfig(enabled=True, max_concurrency=1),
            monotonic=monotonic,
        )
        runtime.start(background=False)
        self.assertEqual(1, runtime.poll_tick())
        self.assertTrue(started.wait(1))
        monotonic.value = 1
        self.assertEqual(0, runtime.poll_tick())
        self.assertEqual(1, backend.claim_calls)

        release.set()
        self.assertTrue(runtime.wait_for_idle(1))
        runtime.shutdown()

    def test_AC_IDX_056_overlapping_poll_is_skipped(self):
        backend = Backend()
        backend.claim_entered, backend.claim_release = Event(), Event()
        runtime = self.runtime(backend)
        runtime.start(background=False)

        first = Thread(target=runtime.poll_tick)
        first.start()
        self.assertTrue(backend.claim_entered.wait(1))
        self.assertEqual(0, runtime.poll_tick())
        self.assertEqual(1, backend.claim_calls)
        backend.claim_release.set()
        first.join(1)
        runtime.shutdown()

    def test_AC_IDX_057_graceful_shutdown_does_not_interrupt_completed_work(self):
        backend = Backend()
        backend.claims.append(Result(200, claim()))
        started, release = Event(), Event()
        seen_cancel: list[Event] = []

        def execute(_claim, cancel):
            seen_cancel.append(cancel)
            started.set()
            release.wait(1)
            backend.operations.append("execute-finished")

        runtime = self.runtime(
            backend,
            execute,
            config=WorkerConfig(enabled=True, max_concurrency=1, shutdown_grace=1),
        )
        runtime.start(background=False)
        runtime.poll_tick()
        self.assertTrue(started.wait(1))

        stopped = Thread(target=runtime.shutdown)
        stopped.start()
        release.set()
        stopped.join(2)

        self.assertFalse(stopped.is_alive())
        self.assertFalse(seen_cancel[0].is_set())
        self.assertFalse(runtime.interrupt_requested)
        self.assertEqual("STOPPED", backend.rows[0].status)
        self.assertLess(
            backend.operations.index("execute-finished"),
            backend.operations.index("stop"),
        )

    def test_AC_IDX_011_active_claim_renews_lease_until_execution_finishes(self):
        backend = Backend()
        backend.claims.append(Result(200, claim()))
        started, release = Event(), Event()

        def execute(_claim, _interrupt):
            started.set()
            release.wait(1)

        runtime = self.runtime(
            backend,
            execute,
            config=WorkerConfig(
                enabled=True,
                max_concurrency=1,
                lease_duration=0.2,
                lease_renew_interval=0.02,
            ),
        )
        runtime.start(background=False)
        runtime.poll_tick()

        self.assertTrue(started.wait(1))
        self.assertTrue(backend.renewed.wait(1))
        self.assertEqual((1, 1, "token"), backend.renewals[0][:3])

        release.set()
        self.assertTrue(runtime.wait_for_idle(1))
        renewals_after_completion = len(backend.renewals)
        self.assertFalse(Event().wait(0.05))
        self.assertEqual(renewals_after_completion, len(backend.renewals))
        runtime.shutdown()

    def test_AC_IDX_041_authoritative_lease_loss_interrupts_only_that_claim(self):
        backend = Backend()
        backend.claims.append(Result(200, claim()))
        backend.claims.append(Result(200, claim(2)))
        backend.renew_errors[1] = PublicError("EMBEDDING-JOB-003")
        first_started, second_started = Event(), Event()
        first_interrupted, second_release = Event(), Event()
        second_interrupts: list[Event] = []

        def execute(claim_data, interrupt):
            if claim_data["jobId"] == 1:
                first_started.set()
                if interrupt.wait(1):
                    first_interrupted.set()
                return
            second_interrupts.append(interrupt)
            second_started.set()
            second_release.wait(1)

        runtime = self.runtime(
            backend,
            execute,
            config=WorkerConfig(
                enabled=True,
                max_concurrency=2,
                lease_duration=0.2,
                lease_renew_interval=0.02,
            ),
        )
        runtime.start(background=False)
        runtime.poll_tick()

        self.assertTrue(first_started.wait(1))
        self.assertTrue(second_started.wait(1))
        self.assertTrue(first_interrupted.wait(1))
        self.assertFalse(second_interrupts[0].is_set())
        second_release.set()
        self.assertTrue(runtime.wait_for_idle(1))
        self.assertFalse(runtime.interrupt_requested)
        runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
