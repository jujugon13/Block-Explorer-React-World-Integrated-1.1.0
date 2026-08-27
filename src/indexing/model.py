"""In-memory relational rows used by the stdlib indexing implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.shared import ChunkRecord


@dataclass(frozen=True, slots=True)
class OperationResult:
    status: int
    data: dict[str, object] | None = None


@dataclass(slots=True)
class WorkerRow:
    id: int
    instance_id: str
    name: str
    hostname: str
    ip: str | None
    status: str
    last_heartbeat: datetime
    created_at: datetime
    stopped_at: datetime | None = None


@dataclass(slots=True)
class DocumentRow:
    id: int
    status: str = "UPLOADED"
    current_version_id: int | None = None
    latest_version_id: int | None = None
    deleted_at: datetime | None = None


@dataclass(slots=True)
class VersionRow:
    id: int
    document_id: int
    version_no: int
    status: str = "UPLOADED"
    indexed_at: datetime | None = None


@dataclass(slots=True)
class JobRow:
    id: int
    document_version_id: int
    status: str
    priority: int
    max_retries: int
    retry_count: int
    created_at: datetime
    next_run_at: datetime | None = None
    worker_id: int | None = None
    claim_token: str | None = None
    locked_at: datetime | None = None
    lease_expires_at: datetime | None = None
    first_started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    failure_type: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class AttemptRow:
    id: int
    job_id: int
    attempt_no: int
    worker_id: int
    claim_token: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    failure_type: str | None = None
    error_message: str | None = None
    failure_result: tuple[str, int, datetime | None] | None = None


@dataclass(slots=True)
class EventRow:
    id: int
    job_id: int
    event_type: str
    occurred_at: datetime
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ModelRow:
    id: int
    name: str
    dimension: int
    active: bool = True
    searchable: bool = True
    provider: str = "OPENAI"
    model_version: str = ""


@dataclass(slots=True)
class VectorRow:
    id: int
    version_id: int
    chunk_index: int
    model_id: int
    values: tuple[float, ...]
    status: str = "ACTIVE"


@dataclass(slots=True)
class IndexingState:
    workers: dict[int, WorkerRow] = field(default_factory=dict)
    documents: dict[int, DocumentRow] = field(default_factory=dict)
    versions: dict[int, VersionRow] = field(default_factory=dict)
    jobs: dict[int, JobRow] = field(default_factory=dict)
    attempts: dict[int, AttemptRow] = field(default_factory=dict)
    events: list[EventRow] = field(default_factory=list)
    chunks: dict[int, tuple[ChunkRecord, ...]] = field(default_factory=dict)
    vectors: list[VectorRow] = field(default_factory=list)
    models: dict[int, ModelRow] = field(default_factory=dict)
