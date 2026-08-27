"""Indexing projections and synchronization-facing operations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import datetime

from src.shared import (
    ChunkRecord,
    Identifier,
    OpsJobSnapshot,
    OpsWorkerSnapshot,
    document_search_id,
    resolve_document_search_id,
)

from .model import AttemptRow, JobRow, ModelRow, VectorRow, VersionRow
from .rules import fail


class QuerySyncMixin:
    def ops_job_snapshots(self, now: datetime) -> tuple[OpsJobSnapshot, ...]:
        del now
        with self._store.read():
            return tuple(
                OpsJobSnapshot(item.status, item.first_started_at, item.completed_at)
                for item in sorted(self._store.list_jobs(), key=lambda row: row.id)
            )

    def ops_worker_snapshots(self, now: datetime) -> tuple[OpsWorkerSnapshot, ...]:
        moment = self._now(now)
        with self._store.read():
            return tuple(
                OpsWorkerSnapshot(self._effective(item, moment), item.last_heartbeat)
                for item in sorted(self._store.list_workers(), key=lambda row: row.id)
            )

    def indexed_document_ids(self) -> frozenset[Identifier]:
        with self._store.read():
            return frozenset(
                document_search_id(document.id)
                for document in self._store.list_documents()
                if document.status == "INDEXED"
                and document.deleted_at is None
                and document.current_version_id is not None
                and (version := self._store.get_version(document.current_version_id)) is not None
                and version.status == "INDEXED"
            )

    def get_indexing_status(self, document_id: Identifier) -> dict[str, object] | None:
        with self._store.read():
            internal_id = resolve_document_search_id(document_id)
            document = self._store.get_document(internal_id) if internal_id else None
            if document is None or document.deleted_at is not None or document.status == "DELETED":
                return None
            latest = self._store.get_version(document.latest_version_id or -1)
            job = next(
                (
                    item
                    for item in self._store.list_jobs()
                    if latest is not None and item.document_version_id == latest.id
                ),
                None,
            )
            return {
                "documentId": document.id,
                "documentStatus": document.status,
                "currentVersionId": document.current_version_id,
                "processingVersion": latest.status if latest is not None else None,
                "jobStatus": job.status if job is not None else None,
            }

    def stale_document_vectors(self, document_id: Identifier) -> None:
        with self._store.transaction():
            version_ids = {
                item.id
                for item in self._store.list_versions()
                if item.document_id == int(document_id)
            }
            for row in self._store.list_vectors():
                if row.version_id in version_ids and row.status == "ACTIVE":
                    row.status = "STALE"
                    self._store.save_vector(row)

    def commit_sync_document_deleted(
        self,
        document_id: Identifier,
        mark_processed: Callable[[], None],
    ) -> None:
        """Atomically stale vectors and complete a deletion event."""

        with self._sync_transaction():
            self.stale_document_vectors(document_id)
            mark_processed()

    def commit_sync_document_version_created(
        self,
        document_id: Identifier,
        version_id: Identifier,
        version_no: int,
        mark_processed: Callable[[], None],
    ) -> None:
        with self._sync_transaction():
            version = self._store.lock_version(int(version_id))
            if (
                version is None
                or version.document_id != int(document_id)
                or version.version_no != version_no
            ):
                fail("SYNC-003")
            jobs = tuple(
                job
                for job in self._store.list_jobs()
                if job.document_version_id == version.id
            )
            if len(jobs) > 1:
                fail("SYNC-003")
            if not jobs:
                self.create_job(version.id)
            mark_processed()

    def commit_sync_document_reindex(
        self,
        version_id: Identifier,
        model_id: Identifier,
        mark_processed: Callable[[], None],
    ) -> None:
        with self._sync_transaction():
            version = self._store.get_version(int(version_id))
            model = self._store.get_model(int(model_id))
            if version is None or model is None or not model.active or not model.searchable:
                fail("SYNC-003")
            jobs = tuple(
                job
                for job in self._store.list_jobs()
                if job.document_version_id == version.id
            )
            if len(jobs) > 1:
                fail("SYNC-003")
            if jobs:
                self.manual_retry(jobs[0].id)
            else:
                version = self._store.lock_version(int(version_id))
                if version is None:
                    fail("SYNC-003")
                self.create_job(version.id)
            mark_processed()

    def commit_sync_model_activated(
        self,
        model_id: Identifier,
        mark_processed: Callable[[], None],
    ) -> None:
        with self._sync_transaction():
            if not self.validate_active_model(model_id):
                fail("SYNC-003")
            mark_processed()

    def validate_active_model(self, model_id: Identifier) -> bool:
        with self._store.read():
            model = self._store.get_model(int(model_id))
            return bool(model and model.active and model.searchable)

    def detail(self, job_id: int) -> dict[str, object]:
        with self._store.read():
            return self._job_detail(self._job(job_id))

    def jobs(
        self,
        *,
        status: str | None = None,
        document_id: int | None = None,
        worker_id: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return the A4 job projection; ownership tokens are never projected."""

        with self._store.read():
            selected: list[dict[str, object]] = []
            for job in sorted(self._store.list_jobs(), key=lambda item: item.id):
                version = self._store.get_version(job.document_version_id)
                if version is None:
                    fail("DOCUMENT-INDEXING-003")
                if (
                    (status is not None and job.status != status)
                    or (document_id is not None and version.document_id != document_id)
                    or (worker_id is not None and job.worker_id != worker_id)
                ):
                    continue
                selected.append(self._job_detail(job))
            return tuple(selected)

    def _job_detail(self, job: JobRow) -> dict[str, object]:
        version = self._store.get_version(job.document_version_id)
        if version is None:
            fail("DOCUMENT-INDEXING-003")
        return {
            "jobId": job.id,
            "documentId": version.document_id,
            "documentVersionId": job.document_version_id,
            "status": job.status,
            "priority": job.priority,
            "retryCount": job.retry_count,
            "maxRetries": job.max_retries,
            "workerId": job.worker_id,
            "lockedAt": job.locked_at,
            "leaseExpiresAt": job.lease_expires_at,
            "firstStartedAt": job.first_started_at,
            "nextRunAt": job.next_run_at,
            "completedAt": job.completed_at,
            "failedAt": job.failed_at,
            "failureType": job.failure_type,
            "errorMessage": job.error_message,
        }

    def attempts(self, job_id: int) -> tuple[dict[str, object], ...]:
        with self._store.read():
            self._job(job_id)
            return tuple(
                self._attempt_data(item)
                for item in sorted(
                    (item for item in self._store.list_attempts() if item.job_id == job_id),
                    key=lambda item: (item.started_at, item.id),
                    reverse=True,
                )
            )

    def events(self, job_id: int) -> tuple[dict[str, object], ...]:
        with self._store.read():
            self._job(job_id)
            return tuple(
                {
                    "eventId": event.id,
                    "jobId": event.job_id,
                    "eventType": event.event_type,
                    "occurredAt": event.occurred_at,
                }
                for event in self._store.list_events()
                if event.job_id == job_id
            )

    def workers(self, now: datetime | None = None) -> tuple[dict[str, object], ...]:
        moment = self._now(now)
        with self._store.read():
            return tuple(
                {
                    "workerId": row.id,
                    "instanceId": row.instance_id,
                    "name": row.name,
                    "status": self._effective(row, moment),
                    "lastHeartbeat": row.last_heartbeat,
                }
                for row in sorted(self._store.list_workers(), key=lambda item: item.id)
            )

    def _active_model(self) -> ModelRow:
        models = [
            item for item in self._store.list_models() if item.active and item.searchable
        ]
        if not models:
            fail("EMBEDDING-MODEL-001")
        if len(models) != 1:
            fail("EMBEDDING-MODEL-002")
        return models[0]

    def _vectors(self, version_id: int, model_id: int) -> tuple[VectorRow, ...]:
        return tuple(
            sorted(
                (
                    row
                    for row in self._store.list_vectors()
                    if row.version_id == version_id and row.model_id == model_id
                ),
                key=lambda row: row.chunk_index,
            )
        )

    @staticmethod
    def _convert_chunks(version_id: int, chunks: Iterable[object]) -> tuple[ChunkRecord, ...]:
        result: list[ChunkRecord] = []
        for expected, item in enumerate(chunks):
            if isinstance(item, str):
                text = item
                record = ChunkRecord(
                    version_id,
                    expected,
                    0,
                    len(text),
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                    len(text.split()),
                )
            else:
                text = str(getattr(item, "text"))
                record = ChunkRecord(
                    version_id,
                    int(getattr(item, "index")),
                    int(getattr(item, "start")),
                    int(getattr(item, "end")),
                    text,
                    str(getattr(item, "text_sha256")),
                    int(getattr(item, "token_estimate")),
                    getattr(item, "page_number", None),
                    getattr(item, "section_title", None),
                )
            if record.index != expected:
                fail("DOCUMENT-CHUNK-001")
            result.append(record)
        return tuple(result)

    @staticmethod
    def _claim_data(job: JobRow) -> dict[str, object]:
        return {
            "jobId": job.id,
            "workerId": job.worker_id,
            "documentVersionId": job.document_version_id,
            "status": job.status,
            "claimToken": job.claim_token,
            "lockedAt": job.locked_at,
            "leaseExpiresAt": job.lease_expires_at,
        }

    @staticmethod
    def _attempt_data(attempt: AttemptRow) -> dict[str, object]:
        return {
            "attemptId": attempt.id,
            "jobId": attempt.job_id,
            "attemptNo": attempt.attempt_no,
            "workerId": attempt.worker_id,
            "status": attempt.status,
            "startedAt": attempt.started_at,
            "endedAt": attempt.ended_at,
            "durationMs": attempt.duration_ms,
            "failureType": attempt.failure_type,
            "errorMessage": attempt.error_message,
        }

    @staticmethod
    def _chunk_data(version_id: int, chunks: tuple[ChunkRecord, ...]) -> dict[str, object]:
        return {"documentVersionId": version_id, "chunkCount": len(chunks)}

    @staticmethod
    def _embedding_data(
        version_id: int, model_id: int, vectors: tuple[VectorRow, ...]
    ) -> dict[str, object]:
        return {
            "documentVersionId": version_id,
            "embeddingModelId": model_id,
            "embeddingCount": len(vectors),
        }

    @staticmethod
    def _completion_data(
        job: JobRow, attempt: AttemptRow, version: VersionRow
    ) -> dict[str, object]:
        return {
            "jobId": job.id,
            "attemptId": attempt.id,
            "documentVersionId": version.id,
            "status": job.status,
            "completedAt": job.completed_at,
        }

    @staticmethod
    def _failure_data(job: JobRow, attempt: AttemptRow) -> dict[str, object]:
        status, retry_count, next_run_at = attempt.failure_result or (
            job.status,
            job.retry_count,
            job.next_run_at,
        )
        return {
            "jobId": job.id,
            "attemptId": attempt.id,
            "status": status,
            "retryCount": retry_count,
            "nextRunAt": next_run_at,
            "failureType": attempt.failure_type,
            "errorMessage": attempt.error_message,
        }
