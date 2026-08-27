"""Small values passed between search handlers and injected adapters."""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID
from src.shared import (
    GuardrailPort,
    IndexCatalog,
    KeywordSearcher,
    LanguageModel,
    PermissionReader,
    QueryEmbedder,
    Reranker,
    SearchCache,
    SearchHistoryWriter,
    SearchHit,
    VectorSearcher,
)


@dataclass(frozen=True, slots=True)
class SearchPorts:
    index_catalog: IndexCatalog
    permissions: PermissionReader
    embedder: QueryEmbedder
    vector: VectorSearcher
    keyword: KeywordSearcher
    llm: LanguageModel
    guardrails: GuardrailPort
    history: SearchHistoryWriter
    cache: SearchCache | None = None
    reranker: Reranker | None = None


@dataclass(frozen=True, slots=True)
class SearchExecution:
    body: dict[str, object]
    headers: tuple[tuple[str, str], ...]
    cacheable: bool = True


def hit_payload(hit: object) -> dict[str, object]:
    if isinstance(hit, SearchHit):
        values = {
            "chunk_id": hit.chunk_id,
            "document_id": hit.document_id,
            "content": hit.content,
            "score": hit.score,
            "metadata": hit.metadata,
        }
    elif isinstance(hit, dict):
        values = dict(hit)
    else:
        values = {
            name: getattr(hit, name)
            for name in ("chunk_id", "document_id", "content", "score", "metadata")
        }
    chunk_id = values.get("chunk_id", values.get("chunkId"))
    document_id = values.get("document_id", values.get("documentId"))
    content = values.get("content")
    score = values.get("score")
    metadata = values.get("metadata")
    if not isinstance(chunk_id, str) or not isinstance(document_id, str):
        raise TypeError("search hit ids must be strings")
    try:
        UUID(chunk_id)
        UUID(document_id)
    except ValueError as error:
        raise TypeError("search hit ids must be UUID strings") from error
    if (
        not isinstance(content, str)
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
    ):
        raise TypeError("search hit content and score are invalid")
    if metadata is not None and not isinstance(metadata, dict):
        metadata = dict(metadata)
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "content": content,
        "score": float(score),
        "metadata": metadata,
    }
