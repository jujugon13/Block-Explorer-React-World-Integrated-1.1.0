"""Atomic indexing completion and failure results."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from src.shared import PublicError

from .model import AttemptRow, JobRow, OperationResult, VersionRow
from .rules import FAILURE_TYPES, LOSS_CODES, RETRYABLE_FAILURES, fail


class ResultsMixin:
    def complete(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: int,
        claim_token: str,
        now: datetime | None = None,
    ) -> OperationResult:
        moment = self._event_time(now)
        with self.document_transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            if job.status == "INDEXED":
                version = self._store.lock_version(job.document_version_id)
                if version is None:
                    fail("DOCUMENT-INDEXING-003")
                result = self._replay_completion(
                    job, attempt_id, worker_id, claim_token
                )
                self._commit_document_result(job, attempt_id, version)
                return result
            self._ownership(job, worker_id, claim_token, self._now(now))
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                fail("DOCUMENT-INDEXING-003")
            document = self._store.lock_document(version.document_id)
            if document is None:
                fail("DOCUMENT-INDEXING-003")
            self._store.lock_attempt(attempt_id)
            attempt = self._started_attempt(attempt_id, job, worker_id, claim_token)
            if version.status != "EMBEDDING":
                fail("DOCUMENT-INDEXING-001")
            if document.deleted_at is not None or document.status not in {
                "UPLOADED",
                "INDEXING",
                "INDEXED",
            }:
                fail("DOCUMENT-INDEXING-001")
            versions = [
                item
                for item in self._store.list_versions()
                if item.document_id == document.id
            ]
            if any(item.version_no > version.version_no for item in versions):
                fail("DOCUMENT-INDEXING-002")
            if document.latest_version_id != version.id:
                fail("DOCUMENT-INDEXING-002")
            if document.current_version_id is not None:
                current = self._store.get_version(document.current_version_id)
                if current is None:
                    fail("DOCUMENT-INDEXING-003")
                if current.version_no > version.version_no:
                    fail("DOCUMENT-INDEXING-002")
            live = sum(
                item.document_version_id == version.id
                and item.status in {"PENDING", "PROCESSING"}
                for item in self._store.list_jobs()
            )
            model = self._active_model()
            chunks = self._store.get_chunks(version.id)
            vectors = self._vectors(version.id, model.id)
            if (
                attempt.started_at >= moment
                or live != 1
                or not chunks
                or len(vectors) != len(chunks)
                or any(
                    row.status != "ACTIVE"
                    or len(row.values) != model.dimension
                    or any(not math.isfinite(value) for value in row.values)
                    for row in vectors
                )
                or any(
                    event.job_id == job.id and event.event_type == "INDEXED"
                    for event in self._store.list_events()
                )
            ):
                fail("DOCUMENT-INDEXING-003")
            for row in self._store.list_vectors():
                other = self._store.get_version(row.version_id)
                if (
                    other is not None
                    and other.document_id == document.id
                    and row.version_id != version.id
                    and row.status == "ACTIVE"
                ):
                    row.status = "STALE"
                    self._store.save_vector(row)
            version.status = "INDEXED"
            version.indexed_at = moment
            document.status = "INDEXED"
            document.current_version_id = version.id
            job.status = "INDEXED"
            job.completed_at = moment
            job.failure_type = None
            job.error_message = None
            attempt.status = "SUCCESS"
            attempt.ended_at = moment
            attempt.duration_ms = int((moment - attempt.started_at).total_seconds() * 1000)
            self._store.save_version(version)
            self._store.save_document(document)
            self._store.save_job(job)
            self._store.save_attempt(attempt)
            self._event(job.id, "INDEXED", moment)
            result = OperationResult(200, self._completion_data(job, attempt, version))
            self._commit_document_result(job, attempt.id, version)
            return result

    def _replay_completion(
        self, job: JobRow, attempt_id: int, worker_id: int, claim_token: str
    ) -> OperationResult:
        if job.worker_id != worker_id or job.claim_token != claim_token:
            fail("EMBEDDING-JOB-003")
        attempt = self._store.get_attempt(attempt_id)
        version = self._store.get_version(job.document_version_id)
        indexed_events = [
            event
            for event in self._store.list_events()
            if event.job_id == job.id and event.event_type == "INDEXED"
        ]
        if (
            attempt is None
            or version is None
            or attempt.job_id != job.id
            or attempt.worker_id != worker_id
            or attempt.claim_token != claim_token
            or attempt.status != "SUCCESS"
            or job.completed_at is None
            or attempt.ended_at != job.completed_at
            or version.status != "INDEXED"
            or version.indexed_at != job.completed_at
            or attempt.duration_ms
            != int((attempt.ended_at - attempt.started_at).total_seconds() * 1000)
            or len(indexed_events) != 1
        ):
            fail("DOCUMENT-INDEXING-003")
        return OperationResult(200, self._completion_data(job, attempt, version))

    def _commit_document_result(
        self, job: JobRow, attempt_id: int, version: VersionRow
    ) -> None:
        if self._document_ledger is None or job.completed_at is None:
            return
        self._document_ledger.commit_index_result(
            job_id=job.id,
            attempt_id=attempt_id,
            version_id=version.id,
            document_id=version.document_id,
            indexed_at=job.completed_at,
        )

    def fail(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: int,
        claim_token: str,
        failure_type: str,
        error_message: str,
        *,
        retry_after: float | timedelta | None = None,
        now: datetime | None = None,
    ) -> OperationResult:
        moment = self._event_time(now)
        with self.document_transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            attempt = self._store.get_attempt(attempt_id)
            if attempt is not None and attempt.status == "FAILED":
                return self._replay_failure(
                    job,
                    attempt,
                    worker_id,
                    claim_token,
                    failure_type,
                    error_message,
                )
            self._ownership(job, worker_id, claim_token, self._now(now))
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                fail("DOCUMENT-INDEXING-004")
            document = self._store.lock_document(version.document_id)
            if document is None:
                fail("DOCUMENT-INDEXING-004")
            self._store.lock_attempt(attempt_id)
            attempt = self._started_attempt(attempt_id, job, worker_id, claim_token)
            if failure_type not in FAILURE_TYPES:
                fail("DOCUMENT-INDEXING-004")
            if version.status in {"UPLOADED", "PARSING"}:
                stage_event = "PARSE_FAILED"
            elif version.status in {"CHUNKED", "EMBEDDING"}:
                stage_event = "EMBEDDING_FAILED"
            else:
                fail("DOCUMENT-INDEXING-004")
            attempt.status = "FAILED"
            attempt.ended_at = moment
            attempt.duration_ms = int((moment - attempt.started_at).total_seconds() * 1000)
            attempt.failure_type = failure_type
            attempt.error_message = error_message
            job.failure_type = failure_type
            job.error_message = error_message
            self._event(job.id, stage_event, moment)
            if failure_type in RETRYABLE_FAILURES and job.retry_count < job.max_retries:
                minimum = self._retry_after_seconds(retry_after)
                delay = self.retry_delay(job.retry_count, minimum)
                job.retry_count += 1
                job.status = "PENDING"
                job.next_run_at = moment + delay
                self._clear_ownership(job)
                self._event(job.id, "RETRY", moment)
            else:
                self._final_failure(job, version, moment)
                self._event(job.id, "FAILED", moment)
            attempt.failure_result = (job.status, job.retry_count, job.next_run_at)
            self._store.save_job(job)
            self._store.save_attempt(attempt)
            result = OperationResult(200, self._failure_data(job, attempt))
            self._commit_document_failure(job, attempt, version)
            return result

    def _replay_failure(
        self,
        job: JobRow,
        attempt: AttemptRow,
        worker_id: int,
        claim_token: str,
        failure_type: str,
        error_message: str,
    ) -> OperationResult:
        if (
            attempt.job_id != job.id
            or attempt.worker_id != worker_id
            or attempt.claim_token != claim_token
        ):
            fail("EMBEDDING-JOB-006")
        if attempt.failure_type != failure_type or attempt.error_message != error_message:
            fail("EMBEDDING-JOB-007")
        if (
            attempt.ended_at is None
            or attempt.failure_result is None
            or attempt.duration_ms
            != int((attempt.ended_at - attempt.started_at).total_seconds() * 1000)
        ):
            fail("DOCUMENT-INDEXING-004")
        return OperationResult(200, self._failure_data(job, attempt))

    def _commit_document_failure(
        self, job: JobRow, attempt: AttemptRow, version: VersionRow
    ) -> None:
        if self._document_ledger is None or attempt.ended_at is None:
            return
        document = self._store.get_document(version.document_id)
        if document is None:
            raise RuntimeError("indexing document is missing")
        self._document_ledger.commit_index_failure(
            job_id=job.id,
            attempt_id=attempt.id,
            version_id=version.id,
            document_id=document.id,
            failed_at=attempt.ended_at,
            job_status=job.status,
            version_status=version.status,
            document_status=document.status,
        )

    def retry_delay(
        self, retry_count: int, provider_minimum: float | None = None
    ) -> timedelta:
        if retry_count < 0 or (
            provider_minimum is not None
            and (not math.isfinite(provider_minimum) or provider_minimum < 0)
        ):
            fail("DOCUMENT-INDEXING-004")
        seconds = self.retry_initial.total_seconds()
        cap = self.retry_max.total_seconds()
        for _ in range(retry_count):
            if seconds * 2 >= cap:
                seconds = cap
                break
            seconds *= 2
        if self.retry_jitter:
            seconds *= self._random_uniform(1 - self.retry_jitter, 1 + self.retry_jitter)
            seconds = min(cap, max(0.001, seconds))
        if provider_minimum is not None and provider_minimum > seconds:
            seconds = provider_minimum
        return timedelta(seconds=seconds)

    @staticmethod
    def _retry_after_seconds(value: float | timedelta | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
        except (TypeError, ValueError, OverflowError):
            fail("DOCUMENT-INDEXING-004")
        if not math.isfinite(seconds) or seconds < 0:
            fail("DOCUMENT-INDEXING-004")
        return seconds

    def report_failure_from_worker(self, *args, **kwargs) -> OperationResult | None:
        """A stale execution withdraws without overwriting the current owner."""
        try:
            return self.fail(*args, **kwargs)
        except PublicError as error:
            if error.code in LOSS_CODES:
                return None
            raise

    def _final_failure(self, job: JobRow, version: VersionRow, moment: datetime) -> None:
        document = self._store.get_document(version.document_id)
        if document is None:
            raise RuntimeError("indexing document is missing")
        job.status = "FAILED"
        job.failed_at = moment
        job.next_run_at = None
        self._clear_ownership(job)
        version.status = "FAILED"
        for row in self._store.list_vectors():
            if row.version_id == version.id and row.status == "ACTIVE":
                row.status = "STALE"
                self._store.save_vector(row)
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
            else "FAILED"
        )
        self._store.save_version(version)
        self._store.save_document(document)

    @staticmethod
    def _clear_ownership(job: JobRow) -> None:
        job.worker_id = None
        job.claim_token = None
        job.locked_at = None
        job.lease_expires_at = None
