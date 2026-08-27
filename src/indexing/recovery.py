"""Operator retry and expired-lease recovery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from src.shared import PublicError

from .model import OperationResult
from .rules import fail


_LOG = logging.getLogger(__name__)


class RecoveryMixin:
    def manual_retry(self, job_id: int, now: datetime | None = None) -> OperationResult:
        moment = self._event_time(now)
        with self.document_transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            if job.status != "FAILED":
                fail("EMBEDDING-JOB-008")
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                fail("EMBEDDING-JOB-009")
            if version.status != "FAILED":
                fail("EMBEDDING-JOB-009")
            document = self._store.lock_document(version.document_id)
            if document is None:
                fail("EMBEDDING-JOB-009")
            if document.deleted_at is not None or document.status == "DELETED":
                fail("EMBEDDING-JOB-009")
            if document.status not in {"UPLOADED", "INDEXING", "INDEXED", "FAILED"}:
                fail("EMBEDDING-JOB-009")
            latest = self._store.get_version(document.latest_version_id or -1)
            if latest is None or latest.document_id != document.id or latest.id != version.id:
                fail("EMBEDDING-JOB-009")
            if any(
                item.id != job.id
                and item.document_version_id == version.id
                and item.status in {"PENDING", "PROCESSING"}
                for item in self._store.list_jobs()
            ):
                fail("EMBEDDING-JOB-008")
            job.status = "PENDING"
            job.next_run_at = None
            job.failed_at = None
            self._clear_ownership(job)
            version.status = "CHUNKED" if self._store.get_chunks(version.id) else "UPLOADED"
            version.indexed_at = None
            previous = (
                self._store.get_version(document.current_version_id)
                if document.current_version_id is not None
                else None
            )
            document.status = (
                "INDEXED"
                if previous is not None
                and previous.id != version.id
                and previous.status == "INDEXED"
                else "UPLOADED"
            )
            self._store.save_job(job)
            self._store.save_version(version)
            self._store.save_document(document)
            self._event(job.id, "MANUAL_RETRY", moment)
            result = OperationResult(200, self.detail(job.id))
            self._commit_document_progress(job, version, moment)
            return result

    def retry_all(
        self,
        now: datetime | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> OperationResult:
        with self._store.read():
            snapshot = tuple(
                sorted(
                    job.id for job in self._store.list_jobs() if job.status == "FAILED"
                )
            )
        retried = skipped = failed = 0
        for job_id in snapshot:
            try:
                self.manual_retry(job_id, now)
                retried += 1
            except PublicError:
                skipped += 1
            except Exception:
                failed += 1
        if retried and on_success is not None:
            on_success()
        data = {
            "scannedCount": len(snapshot),
            "retriedCount": retried,
            "skippedCount": skipped,
            "failedCount": failed,
            "message": f"재처리 {retried}건, 대상 제외 {skipped}건, 오류 {failed}건입니다.",
        }
        return OperationResult(200, data)

    def recover_expired(
        self, now: datetime | None = None, batch_size: int = 100
    ) -> dict[str, int]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        moment = self._now(now)
        cutoff = moment.replace(microsecond=0)
        dead = worker_failures = worker_scan_failures = 0
        try:
            with self._store.read():
                worker_ids = tuple(worker.id for worker in self._store.list_workers())
        except Exception as error:
            worker_ids = ()
            worker_scan_failures = 1
            _LOG.error(
                "dead worker scan failed exception=%s", type(error).__name__
            )
        for worker_id in worker_ids:
            try:
                marked_dead = False
                with self._store.transaction():
                    worker = self._store.lock_worker(worker_id)
                    if (
                        worker is None
                        or worker.status not in {"ACTIVE", "IDLE"}
                        or self._effective(worker, moment) != "DEAD"
                    ):
                        continue
                    worker.status = "DEAD"
                    self._store.save_worker(worker)
                    marked_dead = True
                if marked_dead:
                    dead += 1
            except Exception as error:
                worker_failures += 1
                _LOG.error(
                    "dead worker update failed worker_id=%s exception=%s",
                    worker_id,
                    type(error).__name__,
                )
        try:
            with self._store.transaction():
                candidates = self._store.expired_job_ids(cutoff, batch_size)
        except Exception as error:
            _LOG.error(
                "expired job candidate scan failed exception=%s",
                type(error).__name__,
            )
            summary = {
                "deadWorkers": dead,
                "workerFailures": worker_failures,
                "workerScanFailures": worker_scan_failures,
                "candidateReadFailures": 1,
                "candidates": 0,
                "recovered": 0,
                "skipped": 0,
                "failed": 0,
            }
            _LOG.info("expired recovery summary=%s", summary)
            return summary
        recovered = skipped = failed = 0
        for job_id in candidates:
            try:
                with self.document_transaction():
                    job = self._store.lock_job(job_id)
                    if job is None:
                        skipped += 1
                        continue
                    if (
                        job.status != "PROCESSING"
                        or job.lease_expires_at is None
                        or job.lease_expires_at > cutoff
                    ):
                        skipped += 1
                        continue
                    version = self._store.lock_version(job.document_version_id)
                    if version is None:
                        raise KeyError(job.document_version_id)
                    document = self._store.lock_document(version.document_id)
                    if document is None:
                        raise KeyError(version.document_id)
                    job.failure_type = "WORKER_INTERNAL_ERROR"
                    job.error_message = "lease expired"
                    self._event(job.id, "LEASE_EXPIRED", cutoff)
                    if job.retry_count < job.max_retries:
                        delay = self.retry_delay(job.retry_count)
                        job.retry_count += 1
                        job.status = "PENDING"
                        job.next_run_at = cutoff + delay
                        self._clear_ownership(job)
                        version.status = (
                            "CHUNKED" if self._store.get_chunks(version.id) else "UPLOADED"
                        )
                        version.indexed_at = None
                        self._store.save_version(version)
                        self._event(job.id, "RETRY", cutoff)
                    else:
                        self._final_failure(job, version, cutoff)
                        self._event(job.id, "FAILED", cutoff)
                    self._store.save_job(job)
                    self._commit_document_progress(job, version, cutoff)
                    recovered += 1
            except Exception:
                failed += 1
        summary = {
            "deadWorkers": dead,
            "workerFailures": worker_failures,
            "workerScanFailures": worker_scan_failures,
            "candidateReadFailures": 0,
            "candidates": len(candidates),
            "recovered": recovered,
            "skipped": skipped,
            "failed": failed,
        }
        _LOG.info("expired recovery summary=%s", summary)
        return summary
