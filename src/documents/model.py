from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.shared import StorageLocation


@dataclass(frozen=True, slots=True)
class UploadFile:
    data: bytes
    filename: str | None
    content_type: str | None


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    data: bytes
    filename: str
    content_type: str
    extension: str
    document_type: str
    digest: str

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class FileObject:
    id: int
    digest: str
    size: int
    filename: str
    content_type: str
    document_type: str
    location: StorageLocation


@dataclass(slots=True)
class Document:
    id: int
    title: str
    description: str | None
    document_type: str
    source_type: str
    status: str
    visibility: str
    owner_user_id: int
    owner_name: str
    current_version_id: int | None
    latest_version_id: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


@dataclass(slots=True)
class DocumentVersion:
    id: int
    document_id: int
    version_no: int
    file_object_id: int
    title_snapshot: str
    status: str
    created_at: datetime
    indexed_at: datetime | None = None


@dataclass(slots=True)
class IndexJob:
    id: int
    document_version_id: int
    created_at: datetime
    status: str = "PENDING"


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: int
    event_type: str
    aggregate_id: int


@dataclass(frozen=True, slots=True)
class StoredChunk:
    index: int
    start: int
    end: int
    text: str
    text_sha256: str
    token_estimate: int
    page_number: int | None = None
    section_title: str | None = None


@dataclass(frozen=True, slots=True)
class OperationResult:
    status: int
    data: dict[str, object]


@dataclass(slots=True)
class DocumentState:
    files: dict[int, FileObject] = field(default_factory=dict)
    file_by_digest_size: dict[tuple[str, int], int] = field(default_factory=dict)
    documents: dict[int, Document] = field(default_factory=dict)
    versions: dict[int, DocumentVersion] = field(default_factory=dict)
    version_ids_by_document: dict[int, list[int]] = field(default_factory=dict)
    jobs: dict[int, IndexJob] = field(default_factory=dict)
    job_id_by_version: dict[int, int] = field(default_factory=dict)
    chunks_by_version: dict[int, list[StoredChunk]] = field(default_factory=dict)
    events: list[OutboxEvent] = field(default_factory=list)
