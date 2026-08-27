"""Pure retrieval helpers shared by the mode-search handler."""

from __future__ import annotations

import random
import time
from collections.abc import Iterable, Mapping

from src.shared import SearchUnavailable

from .calls import keyword_search
from .model import SearchPorts, hit_payload


def vector_hits(context: Mapping[str, object], query: str) -> list[dict[str, object]]:
    ports: SearchPorts = context["ports"]  # type: ignore[assignment]
    settings: Mapping[str, object] = context["settings"]  # type: ignore[assignment]
    try:
        vector = ports.embedder.embed_query(query)
        found = ports.vector.search(
            vector,
            context["allowed_document_ids"],  # type: ignore[arg-type]
            int(settings.get("retriever_top_k", 20)),
        )
        return [hit_payload(hit) for hit in found]
    except Exception as error:
        if getattr(error, "code", None) in {
            "EMBEDDING_SERVICE_ERROR",
            "CIRCUIT_BREAKER_OPEN",
        }:
            raise
        raise SearchUnavailable(str(error)) from error


def keyword_hits(context: Mapping[str, object], query: str) -> list[dict[str, object]]:
    ports: SearchPorts = context["ports"]  # type: ignore[assignment]
    settings: Mapping[str, object] = context["settings"]  # type: ignore[assignment]
    found = keyword_search(
        ports.keyword,
        query,
        context["allowed_document_ids"],
        int(settings.get("retriever_top_k", 20)),
        sleep=context.get("sleep", time.sleep),  # type: ignore[arg-type]
        jitter=context.get("jitter", random.uniform),  # type: ignore[arg-type]
    )
    return [hit_payload(hit) for hit in found]


def rrf(
    groups: Iterable[tuple[Iterable[dict[str, object]], float]], constant: float
) -> list[dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    scores: dict[str, float] = {}
    for hits, weight in groups:
        for rank, hit in enumerate(hits):
            chunk_id = str(hit["chunk_id"])
            rows.setdefault(chunk_id, dict(hit))
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (constant + rank + 1)
    for chunk_id, row in rows.items():
        row["score"] = scores[chunk_id]
    return sorted(rows.values(), key=lambda row: float(row["score"]), reverse=True)


def sufficient(results: list[dict[str, object]], settings: Mapping[str, object]) -> bool:
    if not results:
        return False
    top_score = max(float(item["score"]) for item in results)
    by_document: dict[str, float] = {}
    for item in results:
        document_id = str(item["document_id"])
        by_document[document_id] = max(
            by_document.get(document_id, float("-inf")), float(item["score"])
        )
    qualifying = sum(
        score >= float(settings.get("cascading_min_doc_score", 1.0))
        for score in by_document.values()
    )
    return (
        top_score >= float(settings.get("cascading_bm25_threshold", 3.0))
        and qualifying >= int(settings.get("cascading_min_qualifying_docs", 3))
    )
