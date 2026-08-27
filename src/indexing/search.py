"""Vector-search projection over completed in-memory index rows."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from src.shared import (
    Identifier,
    SearchHit,
    chunk_search_id,
    document_search_id,
    resolve_document_search_id,
)

if TYPE_CHECKING:
    from .core import IndexingService


class IndexVectorSearcher:
    """Read only ACTIVE vectors belonging to each document's current version."""

    def __init__(self, service: IndexingService) -> None:
        self._service = service

    def search(
        self,
        vector: Sequence[float],
        document_ids: frozenset[Identifier],
        limit: int,
    ) -> tuple[SearchHit, ...]:
        if isinstance(limit, bool) or limit < 1:
            return ()
        query = tuple(float(value) for value in vector)
        if not query or any(not math.isfinite(value) for value in query):
            raise ValueError("query vector must contain finite values")

        service = self._service
        with service._store.read():
            allowed = {
                resolved
                for identifier in document_ids
                if (resolved := resolve_document_search_id(identifier)) is not None
            }
            if not allowed:
                return ()
            model = service._active_model()
            if len(query) != model.dimension:
                raise ValueError("query vector dimension does not match the active model")
            query_norm = math.hypot(*query)
            hits: list[SearchHit] = []
            for document_id in sorted(allowed):
                document = service._store.get_document(document_id)
                if (
                    document is None
                    or document.status != "INDEXED"
                    or document.deleted_at is not None
                    or document.current_version_id is None
                ):
                    continue
                version = service._store.get_version(document.current_version_id)
                if version is None or version.status != "INDEXED":
                    continue
                chunks = service._store.get_chunks(version.id)
                vectors = service._vectors(version.id, model.id)
                for row in vectors:
                    if row.status != "ACTIVE" or row.chunk_index >= len(chunks):
                        continue
                    row_norm = math.hypot(*row.values)
                    score = (
                        math.fsum(
                            (a / query_norm) * (b / row_norm)
                            for a, b in zip(query, row.values)
                        )
                        if query_norm and row_norm
                        else 0.0
                    )
                    score = max(-1.0, min(1.0, score))
                    chunk = chunks[row.chunk_index]
                    hits.append(
                        SearchHit(
                            chunk_search_id(version.id, chunk.index),
                            document_search_id(document.id),
                            chunk.text,
                            score,
                            {
                                "documentVersionId": version.id,
                                "chunkIndex": chunk.index,
                                "pageNumber": chunk.page_number,
                                "sectionTitle": chunk.section_title,
                            },
                        )
                    )
            hits.sort(key=lambda item: (-item.score, item.chunk_id))
            return tuple(hits[:limit])
