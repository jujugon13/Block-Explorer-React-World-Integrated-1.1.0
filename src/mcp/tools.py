"""MCP read-only tools, validation, shaping, and process-local rate limits."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock

from src.shared import McpDocumentAccess, McpToolBackend, Principal, PublicError

TOOL_LIMITS = {
    "search_documents": 20,
    "get_document_detail": 30,
    "get_indexing_status": 30,
}

TOOL_DEFINITIONS: tuple[dict[str, object], ...] = (
    {
        "name": "search_documents",
        "description": "Search readable documents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "topK": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "get_document_detail",
        "description": "Read a document summary.",
        "inputSchema": {
            "type": "object",
            "properties": {"documentId": {"type": "integer"}},
            "required": ["documentId"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "get_indexing_status",
        "description": "Read a document indexing status.",
        "inputSchema": {
            "type": "object",
            "properties": {"documentId": {"type": "integer"}},
            "required": ["documentId"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
)


@dataclass(slots=True)
class _Window:
    started_at: float
    count: int
    last_accessed_at: float


class ToolRateLimiter:
    """Fixed 60-second windows, intentionally local to one process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._windows: dict[tuple[int, str], _Window] = {}
        self._lock = RLock()

    def acquire(self, user_id: int, tool: str) -> None:
        now = self._clock()
        limit = TOOL_LIMITS[tool]
        key = (user_id, tool)
        with self._lock:
            self._windows = {
                current: window
                for current, window in self._windows.items()
                if now - window.last_accessed_at < 120.0
            }
            window = self._windows.get(key)
            if window is None or now - window.started_at >= 60.0:
                self._windows[key] = _Window(now, 1, now)
                return
            window.last_accessed_at = now
            if window.count >= limit:
                raise PublicError("MCP-001")
            window.count += 1


def omit_null_fields(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): omit_null_fields(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [omit_null_fields(item) for item in value]
    return value


class McpToolService:
    def __init__(
        self,
        backend: McpToolBackend,
        *,
        rate_limiter: ToolRateLimiter | None = None,
    ) -> None:
        self.backend = backend
        self.rate_limiter = rate_limiter or ToolRateLimiter()

    @staticmethod
    def _user_id(principal: Principal) -> int:
        if principal.user_id is None:
            raise PublicError("COMMON-007")
        return principal.user_id

    @staticmethod
    def _arguments(arguments: object) -> Mapping[str, object]:
        if not isinstance(arguments, Mapping):
            raise PublicError("COMMON-002")
        return arguments

    @staticmethod
    def _document_id(arguments: Mapping[str, object]) -> int:
        value = arguments.get("documentId")
        if isinstance(value, bool) or not isinstance(value, int):
            raise PublicError("COMMON-002")
        return value

    @staticmethod
    def _search_arguments(arguments: Mapping[str, object]) -> tuple[str, int]:
        query = arguments.get("query")
        top_k = arguments.get("topK", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000:
            raise PublicError("COMMON-002")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise PublicError("COMMON-002")
        return query, top_k

    @staticmethod
    def _search_result(value: object) -> list[dict[str, object]]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TypeError("search backend must return a sequence")
        output: list[dict[str, object]] = []
        for candidate in value:
            if not isinstance(candidate, Mapping):
                raise TypeError("search result must be an object")
            item = dict(candidate)
            for field in ("content", "chunkText"):
                text = item.get(field)
                if isinstance(text, str):
                    item[field] = text[:1_000]
            output.append(item)
        return output

    def call(
        self, tool: str, arguments: object, principal: Principal
    ) -> object:
        try:
            supplied = self._arguments(arguments)
            user_id = self._user_id(principal)
            if tool == "search_documents":
                query, top_k = self._search_arguments(supplied)
                self.rate_limiter.acquire(user_id, tool)
                result = self._search_result(
                    self.backend.search_documents(principal, query, top_k)
                )
            elif tool in {"get_document_detail", "get_indexing_status"}:
                document_id = self._document_id(supplied)
                self.rate_limiter.acquire(user_id, tool)
                access = self.backend.document_access(principal, document_id)
                if access == McpDocumentAccess.DENIED:
                    raise PublicError("ROLE-002")
                if access == McpDocumentAccess.DELETED:
                    raise PublicError("DOCUMENT-001")
                if access != McpDocumentAccess.ALLOWED:
                    raise TypeError("MCP backend returned an invalid access decision")
                result = (
                    self.backend.get_document_detail(principal, document_id)
                    if tool == "get_document_detail"
                    else self.backend.get_indexing_status(principal, document_id)
                )
            else:
                raise PublicError("COMMON-002")
            return omit_null_fields(result)
        except PublicError:
            raise
        except Exception as error:
            raise PublicError("COMMON-006") from error
