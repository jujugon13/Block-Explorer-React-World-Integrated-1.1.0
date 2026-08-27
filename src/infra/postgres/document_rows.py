"""Adapter-private, data-only rows for the document ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.shared import StorageLocation


@dataclass(slots=True)
class FileObjectRow:
    id: int
    digest: str
    size: int
    filename: str
    content_type: str
    document_type: str
    location: StorageLocation


@dataclass(slots=True)
class DocumentRow:
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
class DocumentVersionRow:
    id: int
    document_id: int
    version_no: int
    file_object_id: int
    title_snapshot: str
    status: str
    created_at: datetime
    indexed_at: datetime | None = None


@dataclass(slots=True)
class IndexJobRow:
    id: int
    document_version_id: int
    created_at: datetime
    status: str = "PENDING"


@dataclass(frozen=True, slots=True, eq=False)
class StoredChunkRow:
    index: int
    start: int
    end: int
    text: str
    text_sha256: str
    token_estimate: int
    page_number: int | None = None
    section_title: str | None = None

    def __eq__(self, other: Any) -> bool:
        names = (
            "index", "start", "end", "text", "text_sha256",
            "token_estimate", "page_number", "section_title",
        )
        return all(getattr(self, name, object()) == getattr(other, name, None) for name in names)

