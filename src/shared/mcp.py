"""Cross-feature contracts used by the external-tool boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .http import Principal


@dataclass(frozen=True, slots=True)
class McpTokenRecord:
    """Persisted token data; the raw API key is deliberately not representable."""

    token_id: str
    owner_user_id: int
    key_sha256: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class McpTokenStore(Protocol):
    """Relational storage boundary for long-lived tool credentials."""

    def insert(self, record: McpTokenRecord) -> None: ...

    def get(self, token_id: str) -> McpTokenRecord | None: ...

    def find_by_hash(self, key_sha256: str) -> McpTokenRecord | None: ...

    def list_for_owner(self, owner_user_id: int) -> Sequence[McpTokenRecord]: ...

    def update(self, record: McpTokenRecord) -> McpTokenRecord: ...

    def touch_last_used_if_active(
        self, token_id: str, key_sha256: str, used_at: datetime
    ) -> bool: ...


class McpDocumentAccess(str, Enum):
    """Non-leaking document authorization decision for MCP detail tools."""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    DELETED = "DELETED"


class McpToolBackend(Protocol):
    """Read-only application ports used by MCP tools."""

    def search_documents(
        self, principal: Principal, query: str, top_k: int
    ) -> Sequence[Mapping[str, object]]: ...

    def document_access(
        self, principal: Principal, document_id: int
    ) -> McpDocumentAccess: ...

    def get_document_detail(
        self, principal: Principal, document_id: int
    ) -> Mapping[str, object]: ...

    def get_indexing_status(
        self, principal: Principal, document_id: int
    ) -> Mapping[str, object]: ...
