"""PostgreSQL persistence adapter for the indexing state machine."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any

from src.shared.indexing import IndexingStore

from .indexing_rows import (
    DOCUMENT_COLUMNS,
    JOB_COLUMNS,
    VERSION_COLUMNS,
    WORKER_COLUMNS,
    document_row,
    job_row,
    version_row,
    worker_row,
)
from .indexing_store_content import PostgresIndexingContentMixin
from .transaction import PostgresTransactionManager


_WORKER_SELECT = ", ".join(WORKER_COLUMNS)
_DOCUMENT_SELECT = ", ".join(DOCUMENT_COLUMNS)
_VERSION_SELECT = ", ".join(VERSION_COLUMNS)
_JOB_SELECT = ", ".join(JOB_COLUMNS)
_ID_SOURCES = {
    "worker": ("indexing_workers", "worker_id"),
    "document": ("documents", "document_id"),
    "version": ("document_versions", "document_version_id"),
    "job": ("indexing_jobs", "job_id"),
    "attempt": ("indexing_attempts", "attempt_id"),
    "event": ("indexing_events", "indexing_event_id"),
    "model": ("embedding_models", "embedding_model_id"),
    "vector": ("document_vectors", "vector_id"),
}


def _close(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


class PostgresIndexingStore(PostgresIndexingContentMixin, IndexingStore):
    """Store records only; IndexingService retains all behavioral decisions."""

    def __init__(self, transactions: PostgresTransactionManager) -> None:
        self._transactions = transactions

    @contextmanager
    def transaction(self):
        with self._transactions.transaction():
            yield

    @contextmanager
    def read(self):
        with self._transactions.operation():
            yield

    def _cursor(self):
        return self._transactions.current_connection().cursor()

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        cursor = self._cursor()
        try:
            cursor.execute(sql, parameters)
        finally:
            _close(cursor)

    def _executemany(
        self, sql: str, parameter_rows: tuple[tuple[object, ...], ...]
    ) -> None:
        if not parameter_rows:
            return
        cursor = self._cursor()
        try:
            cursor.executemany(sql, parameter_rows)
        finally:
            _close(cursor)

    def _fetchone(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> Any | None:
        cursor = self._cursor()
        try:
            cursor.execute(sql, parameters)
            return cursor.fetchone()
        finally:
            _close(cursor)

    def _fetchall(
        self, sql: str, parameters: tuple[object, ...] = ()
    ) -> tuple[Any, ...]:
        cursor = self._cursor()
        try:
            cursor.execute(sql, parameters)
            return tuple(cursor.fetchall())
        finally:
            _close(cursor)

    def next_id(self, kind: str, requested: int | None = None) -> int:
        table, column = _ID_SOURCES[kind]
        if requested is not None:
            existing = self._fetchone(
                f"SELECT 1 FROM {table} WHERE {column} = %s",
                (requested,),
            )
            if existing is not None:
                return requested
            while True:
                row = self._fetchone(
                    "SELECT nextval(pg_get_serial_sequence(%s, %s))",
                    (table, column),
                )
                if row is None:
                    raise RuntimeError(
                        f"identity sequence is unavailable for {kind}"
                    )
                allocated = int(
                    next(iter(row.values())) if hasattr(row, "values") else row[0]
                )
                if allocated >= requested:
                    break
            return requested
        row = self._fetchone(
            "SELECT nextval(pg_get_serial_sequence(%s, %s))",
            (table, column),
        )
        if row is None:
            raise RuntimeError(f"identity sequence is unavailable for {kind}")
        value = next(iter(row.values())) if hasattr(row, "values") else row[0]
        return int(value)

    def lock_worker_registration(self, instance_id: str) -> None:
        self._execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (instance_id,),
        )

    def get_worker(self, worker_id: int):
        row = self._fetchone(
            f"SELECT {_WORKER_SELECT} FROM indexing_workers WHERE worker_id = %s",
            (worker_id,),
        )
        return worker_row(row) if row is not None else None

    def lock_worker(self, worker_id: int):
        row = self._fetchone(
            f"SELECT {_WORKER_SELECT} FROM indexing_workers "
            "WHERE worker_id = %s FOR UPDATE",
            (worker_id,),
        )
        return worker_row(row) if row is not None else None

    def list_workers(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_WORKER_SELECT} FROM indexing_workers ORDER BY worker_id"
        )
        return tuple(worker_row(row) for row in rows)

    def insert_worker(self, worker: Any) -> None:
        self._execute(
            """
            INSERT INTO indexing_workers (
                worker_id, instance_id, name, hostname, ip, status,
                last_heartbeat, created_at, stopped_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                worker.id, worker.instance_id, worker.name, worker.hostname, worker.ip,
                worker.status, worker.last_heartbeat, worker.created_at, worker.stopped_at,
            ),
        )

    def save_worker(self, worker: Any) -> None:
        self._execute(
            """
            UPDATE indexing_workers
               SET status = %s, last_heartbeat = %s, stopped_at = %s
             WHERE worker_id = %s
            """,
            (worker.status, worker.last_heartbeat, worker.stopped_at, worker.id),
        )

    def get_document(self, document_id: int):
        row = self._fetchone(
            f"SELECT {_DOCUMENT_SELECT} FROM documents WHERE document_id = %s",
            (document_id,),
        )
        return document_row(row) if row is not None else None

    def lock_document(self, document_id: int):
        row = self._fetchone(
            f"SELECT {_DOCUMENT_SELECT} FROM documents "
            "WHERE document_id = %s FOR UPDATE",
            (document_id,),
        )
        return document_row(row) if row is not None else None

    def list_documents(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_DOCUMENT_SELECT} FROM documents ORDER BY document_id"
        )
        return tuple(document_row(row) for row in rows)

    def insert_document(self, document: Any) -> None:
        if self.lock_document(document.id) is None:
            raise RuntimeError("document row must be created by the document store")
        self.save_document(document)

    def save_document(self, document: Any) -> None:
        self._execute(
            """
            UPDATE documents
               SET status = %s,
                   current_version_id = %s,
                   latest_version_id = %s,
                   deleted_at = %s
             WHERE document_id = %s
            """,
            (
                document.status, document.current_version_id,
                document.latest_version_id, document.deleted_at, document.id,
            ),
        )

    def get_version(self, version_id: int):
        row = self._fetchone(
            f"SELECT {_VERSION_SELECT} FROM document_versions "
            "WHERE document_version_id = %s",
            (version_id,),
        )
        return version_row(row) if row is not None else None

    def lock_version(self, version_id: int):
        row = self._fetchone(
            f"SELECT {_VERSION_SELECT} FROM document_versions "
            "WHERE document_version_id = %s FOR UPDATE",
            (version_id,),
        )
        return version_row(row) if row is not None else None

    def list_versions(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_VERSION_SELECT} FROM document_versions ORDER BY document_version_id"
        )
        return tuple(version_row(row) for row in rows)

    def insert_version(self, version: Any) -> None:
        existing = self.lock_version(version.id)
        if existing is None:
            raise RuntimeError("version row must be created by the document store")
        if (
            existing.document_id != version.document_id
            or existing.version_no != version.version_no
        ):
            raise RuntimeError("document version identity mismatch")
        self.save_version(version)

    def save_version(self, version: Any) -> None:
        self._execute(
            """
            UPDATE document_versions
               SET status = %s, indexed_at = %s
             WHERE document_version_id = %s
            """,
            (version.status, version.indexed_at, version.id),
        )

    def get_job(self, job_id: int):
        row = self._fetchone(
            f"SELECT {_JOB_SELECT} FROM indexing_jobs WHERE job_id = %s",
            (job_id,),
        )
        return job_row(row) if row is not None else None

    def lock_job(self, job_id: int):
        row = self._fetchone(
            f"SELECT {_JOB_SELECT} FROM indexing_jobs "
            "WHERE job_id = %s FOR UPDATE",
            (job_id,),
        )
        return job_row(row) if row is not None else None

    def list_jobs(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_JOB_SELECT} FROM indexing_jobs ORDER BY job_id"
        )
        return tuple(job_row(row) for row in rows)

    def lock_next_pending_job(self, now: datetime):
        row = self._fetchone(
            f"""
            SELECT {_JOB_SELECT}
              FROM indexing_jobs
             WHERE status = 'PENDING'
               AND (next_run_at IS NULL OR next_run_at <= %s)
             ORDER BY priority DESC, created_at ASC, job_id ASC
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """,
            (now,),
        )
        return job_row(row) if row is not None else None

    def expired_job_ids(self, cutoff: datetime, batch_size: int) -> tuple[int, ...]:
        rows = self._fetchall(
            """
            SELECT job_id
              FROM indexing_jobs
             WHERE status = 'PROCESSING'
               AND lease_expires_at IS NOT NULL
               AND lease_expires_at <= %s
             ORDER BY lease_expires_at ASC, job_id ASC
             LIMIT %s
            """,
            (cutoff, batch_size),
        )
        return tuple(
            int(next(iter(row.values())) if hasattr(row, "values") else row[0])
            for row in rows
        )

    def insert_job(self, job: Any) -> None:
        self._execute(
            """
            INSERT INTO indexing_jobs (
                job_id, document_version_id, status, priority, max_retries,
                retry_count, created_at, next_run_at, worker_id, claim_token,
                locked_at, lease_expires_at, first_started_at, completed_at,
                failed_at, failure_type, error_message
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            self._job_parameters(job),
        )

    @staticmethod
    def _job_parameters(job: Any) -> tuple[object, ...]:
        return (
            job.id, job.document_version_id, job.status, job.priority,
            job.max_retries, job.retry_count, job.created_at, job.next_run_at,
            job.worker_id, job.claim_token, job.locked_at, job.lease_expires_at,
            job.first_started_at, job.completed_at, job.failed_at,
            job.failure_type, job.error_message,
        )

    def save_job(self, job: Any) -> None:
        self._execute(
            """
            UPDATE indexing_jobs
               SET status = %s,
                   priority = %s,
                   max_retries = %s,
                   retry_count = %s,
                   next_run_at = %s,
                   worker_id = %s,
                   claim_token = %s,
                   locked_at = %s,
                   lease_expires_at = %s,
                   first_started_at = %s,
                   completed_at = %s,
                   failed_at = %s,
                   failure_type = %s,
                   error_message = %s
             WHERE job_id = %s
            """,
            (
                job.status, job.priority, job.max_retries, job.retry_count,
                job.next_run_at, job.worker_id, job.claim_token, job.locked_at,
                job.lease_expires_at, job.first_started_at, job.completed_at,
                job.failed_at, job.failure_type, job.error_message, job.id,
            ),
        )
