"""Composition adapter from VectorShelf domain services to the MCP tool port."""

from __future__ import annotations

from typing import Protocol

from src.shared import McpDocumentAccess, Principal


class _Search(Protocol):
    def execute(self, payload: object, principal: Principal, *, debug: bool = False): ...


class _Documents(Protocol):
    def detail(self, principal: Principal, document_id: int) -> dict[str, object]: ...

    def status(self, principal: Principal, document_id: int) -> dict[str, object]: ...


class _Permissions(Protocol):
    def mcp_document_access(
        self, principal: Principal, document_id: int
    ) -> McpDocumentAccess: ...


class McpApplicationBackend:
    """Use the same search, document, and permission services as the REST API."""

    def __init__(
        self,
        search: _Search,
        documents: _Documents,
        permissions: _Permissions,
    ) -> None:
        self._search = search
        self._documents = documents
        self._permissions = permissions

    def search_documents(
        self, principal: Principal, query: str, top_k: int
    ) -> list[dict[str, object]]:
        execution = self._search.execute(
            {"query": query, "top_k": top_k, "generate_answer": False},
            principal,
        )
        results = execution.body.get("results")
        if not isinstance(results, list) or not all(
            isinstance(item, dict) for item in results
        ):
            raise TypeError("search service returned invalid results")
        return [dict(item) for item in results[:top_k]]

    def document_access(
        self, principal: Principal, document_id: int
    ) -> McpDocumentAccess:
        return self._permissions.mcp_document_access(principal, document_id)

    def get_document_detail(
        self, principal: Principal, document_id: int
    ) -> dict[str, object]:
        return self._documents.detail(principal, document_id)

    def get_indexing_status(
        self, principal: Principal, document_id: int
    ) -> dict[str, object]:
        return self._documents.status(principal, document_id)
