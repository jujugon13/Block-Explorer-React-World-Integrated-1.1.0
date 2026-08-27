"""Worker registry, liveness, claim, lease, and attempt ownership."""

from __future__ import annotations

from datetime import datetime

from .model import AttemptRow, JobRow, OperationResult, WorkerRow
from .rules import fail


class OwnershipMixin:
    def register_worker(
        self,
        instance_id: str,
        name: str = "indexing-worker",
        hostname: str = "unknown",
        ip: str | None = None,
        now: datetime | None = None,
    ) -> WorkerRow:
        moment = self._now(now)
        with self._store.transaction():
            self._store.lock_worker_registration(instance_id)
            duplicate = any(
                row.instance_id == instance_id for row in self._store.list_workers()
            )
            row = WorkerRow(
                self._id("worker"),
                instance_id,
                name,
                hostname,
                ip,
                "STOPPED" if duplicate else "ACTIVE",
                moment,
                moment,
                moment if duplicate else None,
            )
            self._store.insert_worker(row)
            return row

    def heartbeat(self, worker_id: int, now: datetime | None = None) -> None:
        with self._store.transaction():
            worker = self._store.lock_worker(worker_id)
            if worker is None:
                fail("WORKER-001")
            if worker.status not in {"ACTIVE", "IDLE"}:
                fail("WORKER-002")
            worker.last_heartbeat = self._now(now)
            self._store.save_worker(worker)

    def stop_worker(self, worker_id: int, now: datetime | None = None) -> None:
        with self._store.transaction():
            worker = self._store.lock_worker(worker_id)
            if worker is None:
                fail("WORKER-001")
            worker.status = "STOPPED"
            worker.stopped_at = self._now(now)
            self._store.save_worker(worker)

    def effective_worker_status(self, worker_id: int, now: datetime | None = None) -> str:
        with self._store.read():
            worker = self._store.get_worker(worker_id)
            if worker is None:
                fail("WORKER-001")
            return self._effective(worker, self._now(now))

    def _effective(self, worker: WorkerRow, now: datetime) -> str:
        if worker.status in {"STOPPED", "DEAD"}:
            return worker.status
        return worker.status if worker.last_heartbeat > now - self.dead_threshold else "DEAD"

    def claim(self, worker_id: int, now: datetime | None = None) -> OperationResult:
        moment = self._now(now)
        with self.document_transaction():
            worker = self._store.get_worker(worker_id)
            if worker is None:
                fail("WORKER-001")
            if self._effective(worker, moment) not in {"ACTIVE", "IDLE"}:
                fail("WORKER-002")
            job = self._store.lock_next_pending_job(moment)
            if job is None:
                return OperationResult(204)
            worker = self._store.lock_worker(worker_id)
            if worker is None:
                fail("WORKER-001")
            if self._effective(worker, moment) not in {"ACTIVE", "IDLE"}:
                fail("WORKER-002")
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                raise KeyError(job.document_version_id)
            document = self._store.lock_document(version.document_id)
            if document is None:
                raise KeyError(version.document_id)
            job.status = "PROCESSING"
            job.worker_id = worker_id
            job.claim_token = str(self._uuid_factory())
            job.locked_at = moment
            job.lease_expires_at = moment + self.lease_duration
            if job.first_started_at is None:
                job.first_started_at = moment
            if document.status == "UPLOADED":
                document.status = "INDEXING"
            self._store.save_job(job)
            self._store.save_document(document)
            self._event(job.id, "LOCKED", moment)
            result = OperationResult(200, self._claim_data(job))
            self._commit_document_progress(job, version, moment)
            return result

    def _job(self, job_id: int) -> JobRow:
        job = self._store.get_job(job_id)
        if job is None:
            fail("EMBEDDING-JOB-001")
        return job

    def _ownership(
        self, job: JobRow, worker_id: int, claim_token: str, now: datetime
    ) -> None:
        if job.status != "PROCESSING":
            fail("EMBEDDING-JOB-002")
        if (
            job.worker_id is None
            or job.claim_token is None
            or job.locked_at is None
            or job.lease_expires_at is None
        ):
            fail("EMBEDDING-JOB-005")
        if job.worker_id != worker_id or job.claim_token != claim_token:
            fail("EMBEDDING-JOB-003")
        if now >= job.lease_expires_at:
            fail("EMBEDDING-JOB-004")

    def renew(
        self,
        job_id: int,
        worker_id: int,
        claim_token: str,
        now: datetime | None = None,
    ) -> OperationResult:
        moment = self._now(now)
        with self._store.transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            self._ownership(job, worker_id, claim_token, moment)
            worker = self._store.lock_worker(worker_id)
            if worker is None:
                fail("WORKER-001")
            if self._effective(worker, moment) not in {"ACTIVE", "IDLE"}:
                fail("WORKER-002")
            new_expiry = moment + self.lease_duration
            if job.lease_expires_at is None or new_expiry <= job.lease_expires_at:
                fail("EMBEDDING-JOB-005")
            job.lease_expires_at = new_expiry
            self._store.save_job(job)
            return OperationResult(
                200, {"jobId": job.id, "leaseExpiresAt": job.lease_expires_at}
            )

    def start_attempt(
        self,
        job_id: int,
        worker_id: int,
        claim_token: str,
        now: datetime | None = None,
    ) -> OperationResult:
        moment = self._now(now)
        with self._store.transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            self._ownership(job, worker_id, claim_token, moment)
            existing = next(
                (
                    item
                    for item in self._store.list_attempts()
                    if item.job_id == job_id and item.claim_token == claim_token
                ),
                None,
            )
            if existing is not None:
                if existing.worker_id != worker_id:
                    fail("EMBEDDING-JOB-005")
                return OperationResult(200, self._attempt_data(existing))
            number = 1 + max(
                (
                    item.attempt_no
                    for item in self._store.list_attempts()
                    if item.job_id == job_id
                ),
                default=0,
            )
            attempt = AttemptRow(
                self._id("attempt"),
                job_id,
                number,
                worker_id,
                claim_token,
                "STARTED",
                moment,
            )
            self._store.insert_attempt(attempt)
            return OperationResult(201, self._attempt_data(attempt))

    def _started_attempt(
        self,
        attempt_id: int,
        job: JobRow,
        worker_id: int,
        claim_token: str,
    ) -> AttemptRow:
        attempt = self._store.get_attempt(attempt_id)
        if (
            attempt is None
            or attempt.job_id != job.id
            or attempt.worker_id != worker_id
            or attempt.claim_token != claim_token
            or attempt.status != "STARTED"
        ):
            fail("EMBEDDING-JOB-006")
        return attempt
