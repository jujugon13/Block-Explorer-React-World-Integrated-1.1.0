"""Cross-feature contracts for document indexing and query embedding."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .access import Identifier
from .storage import StorageLocation


@dataclass(frozen=True, slots=True)
class VersionFileSnapshot:
    document_id: Identifier
    version_id: Identifier
    version_no: int
    version_status: str
    document_status: str
    document_type: str
    file_location: StorageLocation


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    version_id: Identifier
    index: int
    start: int
    end: int
    text: str
    text_sha256: str
    token_estimate: int
    page_number: int | None = None
    section_title: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentVersionRegistration:
    """Identifiers and initial states shared by document and indexing ledgers."""

    document_id: Identifier
    document_status: str
    current_version_id: Identifier | None
    latest_version_id: Identifier
    deleted_at: datetime | None
    version_id: Identifier
    version_no: int
    version_status: str
    job_id: Identifier
    created_at: datetime


class IndexCatalog(Protocol):
    def indexed_document_ids(self) -> frozenset[Identifier]: ...

    def get_indexing_status(self, document_id: Identifier) -> dict[str, object] | None: ...


class QueryEmbedder(Protocol):
    def embed_query(self, text: str) -> tuple[float, ...]: ...


class DocumentIndexPort(Protocol):
    """Short prepare/commit operations around out-of-transaction file I/O."""

    def snapshot_for_chunking(self, version_id: Identifier) -> VersionFileSnapshot: ...

    def save_chunks(
        self, version_id: Identifier, chunks: tuple[ChunkRecord, ...]
    ) -> None: ...

    def chunks_for_embedding(self, version_id: Identifier) -> tuple[ChunkRecord, ...]: ...


class IndexingUnitOfWork(Protocol):
    """One atomic completion/failure boundary for all indexing-owned rows."""

    def commit_index_progress(
        self,
        *,
        job_id: Identifier,
        version_id: Identifier,
        document_id: Identifier,
        changed_at: datetime,
        job_status: str,
        version_status: str,
        document_status: str,
    ) -> None: ...

    def commit_index_result(
        self,
        *,
        job_id: Identifier,
        attempt_id: Identifier,
        version_id: Identifier,
        document_id: Identifier,
        indexed_at: datetime,
    ) -> None: ...

    def commit_index_failure(
        self,
        *,
        job_id: Identifier,
        attempt_id: Identifier,
        version_id: Identifier,
        document_id: Identifier,
        failed_at: datetime,
        job_status: str,
        version_status: str,
        document_status: str,
    ) -> None: ...


class DocumentIndexLedger(DocumentIndexPort, IndexingUnitOfWork, Protocol):
    """Document-owned rows needed by indexing workers and terminal commits."""


class DocumentIndexParticipant(Protocol):
    """Indexing-owned participant in document/version creation transactions."""

    def bind_document_ledger(self, ledger: DocumentIndexLedger) -> None: ...

    def document_transaction(self) -> AbstractContextManager[None]: ...

    def register_document_version(
        self, registration: DocumentVersionRegistration
    ) -> None: ...


class IndexingStore(Protocol):
    """Persistence-only seam for the indexing ledger.

    Implementations return mutable row records.  The indexing service retains
    every validation, state transition, error mapping, and lock call order; a
    store only loads, locks, allocates, and persists those records.

    ``lock_next_pending_job`` is the one selection primitive that must be
    specialized by a relational adapter.  PostgreSQL implementations use the
    FR-IDX-060 order and ``FOR UPDATE SKIP LOCKED LIMIT 1`` without changing the
    selected row.  Recovery deliberately uses an unlocked ID snapshot followed
    by one ``lock_job`` transaction per candidate (FR-IDX-103).
    """

    def transaction(self) -> AbstractContextManager[None]: ...

    def read(self) -> AbstractContextManager[None]: ...

    def next_id(self, kind: str, requested: int | None = None) -> int: ...

    def lock_worker_registration(self, instance_id: str) -> None: ...

    def get_worker(self, worker_id: int) -> Any | None: ...

    def lock_worker(self, worker_id: int) -> Any | None: ...

    def list_workers(self) -> tuple[Any, ...]: ...

    def insert_worker(self, worker: Any) -> None: ...

    def save_worker(self, worker: Any) -> None: ...

    def get_document(self, document_id: int) -> Any | None: ...

    def lock_document(self, document_id: int) -> Any | None: ...

    def list_documents(self) -> tuple[Any, ...]: ...

    def insert_document(self, document: Any) -> None: ...

    def save_document(self, document: Any) -> None: ...

    def get_version(self, version_id: int) -> Any | None: ...

    def lock_version(self, version_id: int) -> Any | None: ...

    def list_versions(self) -> tuple[Any, ...]: ...

    def insert_version(self, version: Any) -> None: ...

    def save_version(self, version: Any) -> None: ...

    def get_job(self, job_id: int) -> Any | None: ...

    def lock_job(self, job_id: int) -> Any | None: ...

    def list_jobs(self) -> tuple[Any, ...]: ...

    def lock_next_pending_job(self, now: datetime) -> Any | None: ...

    def expired_job_ids(self, cutoff: datetime, batch_size: int) -> tuple[int, ...]: ...

    def insert_job(self, job: Any) -> None: ...

    def save_job(self, job: Any) -> None: ...

    def get_attempt(self, attempt_id: int) -> Any | None: ...

    def lock_attempt(self, attempt_id: int) -> Any | None: ...

    def list_attempts(self) -> tuple[Any, ...]: ...

    def insert_attempt(self, attempt: Any) -> None: ...

    def save_attempt(self, attempt: Any) -> None: ...

    def list_events(self) -> tuple[Any, ...]: ...

    def insert_event(self, event: Any) -> None: ...

    def get_model(self, model_id: int) -> Any | None: ...

    def list_models(self) -> tuple[Any, ...]: ...

    def insert_model(self, model: Any) -> None: ...

    def save_model(self, model: Any) -> None: ...

    def get_chunks(self, version_id: int) -> tuple[ChunkRecord, ...]: ...

    def save_chunks(self, version_id: int, chunks: tuple[ChunkRecord, ...]) -> None: ...

    def list_vectors(self) -> tuple[Any, ...]: ...

    def insert_vectors(self, vectors: tuple[Any, ...]) -> None: ...

    def save_vector(self, vector: Any) -> None: ...
