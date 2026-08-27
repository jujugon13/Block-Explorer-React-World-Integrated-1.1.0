"""Private structural rows used by the PostgreSQL indexing adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _value(row: Any, columns: tuple[str, ...], name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[columns.index(name)]


@dataclass(slots=True)
class _WorkerRow:
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
class _DocumentRow:
    id: int
    status: str
    current_version_id: int | None
    latest_version_id: int | None
    deleted_at: datetime | None


@dataclass(slots=True)
class _VersionRow:
    id: int
    document_id: int
    version_no: int
    status: str
    indexed_at: datetime | None = None


@dataclass(slots=True)
class _JobRow:
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
class _AttemptRow:
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
class _EventRow:
    id: int
    job_id: int
    event_type: str
    occurred_at: datetime
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class _ModelRow:
    id: int
    name: str
    dimension: int
    active: bool = True
    searchable: bool = True
    provider: str | None = None
    model_version: str | None = None


@dataclass(slots=True)
class _VectorRow:
    id: int
    version_id: int
    chunk_index: int
    model_id: int
    values: tuple[float, ...]
    status: str = "ACTIVE"


WORKER_COLUMNS = (
    "worker_id", "instance_id", "name", "hostname", "ip", "status",
    "last_heartbeat", "created_at", "stopped_at",
)
DOCUMENT_COLUMNS = (
    "document_id", "status", "current_version_id", "latest_version_id", "deleted_at",
)
VERSION_COLUMNS = (
    "document_version_id", "document_id", "version_no", "status", "indexed_at",
)
JOB_COLUMNS = (
    "job_id", "document_version_id", "status", "priority", "max_retries",
    "retry_count", "created_at", "next_run_at", "worker_id", "claim_token",
    "locked_at", "lease_expires_at", "first_started_at", "completed_at",
    "failed_at", "failure_type", "error_message",
)
ATTEMPT_COLUMNS = (
    "attempt_id", "job_id", "attempt_no", "worker_id", "claim_token", "status",
    "started_at", "ended_at", "duration_ms", "failure_type", "error_message",
    "result_job_status", "result_retry_count", "result_next_run_at",
)
EVENT_COLUMNS = (
    "indexing_event_id", "job_id", "event_type", "occurred_at", "metadata",
)
MODEL_COLUMNS = (
    "embedding_model_id", "name", "dimension", "active", "searchable",
    "provider", "model_version",
)
VECTOR_COLUMNS = (
    "vector_id", "document_version_id", "chunk_index", "embedding_model_id",
    "embedding_text", "status",
)


def worker_row(row: Any) -> _WorkerRow:
    return _WorkerRow(*(_value(row, WORKER_COLUMNS, name) for name in WORKER_COLUMNS))


def document_row(row: Any) -> _DocumentRow:
    return _DocumentRow(*(_value(row, DOCUMENT_COLUMNS, name) for name in DOCUMENT_COLUMNS))


def version_row(row: Any) -> _VersionRow:
    return _VersionRow(*(_value(row, VERSION_COLUMNS, name) for name in VERSION_COLUMNS))


def job_row(row: Any) -> _JobRow:
    values = [_value(row, JOB_COLUMNS, name) for name in JOB_COLUMNS]
    values[9] = str(values[9]) if values[9] is not None else None
    return _JobRow(*values)


def attempt_row(row: Any) -> _AttemptRow:
    values = [_value(row, ATTEMPT_COLUMNS, name) for name in ATTEMPT_COLUMNS]
    result = (
        (str(values[11]), int(values[12]), values[13])
        if values[11] is not None and values[12] is not None
        else None
    )
    return _AttemptRow(
        int(values[0]), int(values[1]), int(values[2]), int(values[3]),
        str(values[4]), str(values[5]), values[6], values[7], values[8],
        values[9], values[10], result,
    )


def event_row(row: Any) -> _EventRow:
    values = [_value(row, EVENT_COLUMNS, name) for name in EVENT_COLUMNS]
    metadata = values[4]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if not isinstance(metadata, Mapping):
        raise TypeError("indexing event metadata must be an object")
    return _EventRow(
        int(values[0]), int(values[1]), str(values[2]), values[3], dict(metadata)
    )


def model_row(row: Any) -> _ModelRow:
    return _ModelRow(*(_value(row, MODEL_COLUMNS, name) for name in MODEL_COLUMNS))


def parse_vector(value: object) -> tuple[float, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(float(item) for item in value)
    text = str(value).strip()
    if not text.startswith("[") or not text.endswith("]"):
        raise ValueError("invalid pgvector text")
    body = text[1:-1].strip()
    return tuple(float(item) for item in body.split(",")) if body else ()


def vector_text(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def vector_row(row: Any) -> _VectorRow:
    values = [_value(row, VECTOR_COLUMNS, name) for name in VECTOR_COLUMNS]
    return _VectorRow(
        int(values[0]), int(values[1]), int(values[2]), int(values[3]),
        parse_vector(values[4]), str(values[5]),
    )
