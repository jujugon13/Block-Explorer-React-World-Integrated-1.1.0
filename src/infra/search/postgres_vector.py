"""PostgreSQL cosine vector search over the active document version."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from src.shared import (
    Identifier,
    SearchHit,
    SearchUnavailable,
    chunk_search_id,
    document_search_id,
    resolve_document_search_id,
)


VECTOR_DIMENSION = 1536
MODEL_PROVIDER = "OPENAI"
MODEL_NAME = "text-embedding-3-small"
MODEL_VERSION = "text-embedding-3-small"

_MODEL_SQL = """
SELECT embedding_model_id
  FROM embedding_models
 WHERE provider = %s
   AND model_name = %s
   AND model_version = %s
   AND dimension = %s
   AND active
   AND searchable
 ORDER BY embedding_model_id
"""

_SEARCH_SQL = """
WITH nearest AS MATERIALIZED (
    SELECT dv.vector_id,
           dv.document_version_id,
           dv.chunk_index,
           d.document_id,
           c.content,
           c.page_number,
           c.section_title,
           dv.embedding::vector(1536) <=> %s::vector(1536) AS distance
      FROM document_vectors dv
      JOIN document_versions v
        ON v.document_version_id = dv.document_version_id
      JOIN documents d
        ON d.document_id = v.document_id
       AND d.current_version_id = v.document_version_id
      JOIN document_chunks c
        ON c.document_version_id = dv.document_version_id
       AND c.chunk_index = dv.chunk_index
     WHERE dv.embedding_model_id = {model_id}
       AND dv.status = 'ACTIVE'
       AND d.document_id = ANY(%s::bigint[])
       AND d.status = 'INDEXED'
       AND d.deleted_at IS NULL
       AND v.status = 'INDEXED'
     ORDER BY dv.embedding::vector(1536) <=> %s::vector(1536)
     LIMIT %s
)
SELECT vector_id, document_version_id, chunk_index, document_id,
       content, page_number, section_title, distance
  FROM nearest
 ORDER BY distance, vector_id
"""


def _close(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


def _all(connection: Any, sql: str, parameters: object = ()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return cursor.fetchall()
    finally:
        _close(cursor)


def _run(connection: Any, sql: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        _close(cursor)


class PostgresVectorSearcher:
    """Use pgvector HNSW without opening a second connection boundary."""

    def __init__(self, transactions: Any) -> None:
        self.transactions = transactions

    def search(
        self,
        vector: Sequence[float],
        document_ids: frozenset[Identifier],
        limit: int,
    ) -> tuple[SearchHit, ...]:
        if isinstance(limit, bool) or limit < 1:
            return ()
        allowed = sorted(
            {
                resolved
                for identifier in document_ids
                if (resolved := resolve_document_search_id(identifier)) is not None
            }
        )
        if not allowed:
            return ()

        query = tuple(float(value) for value in vector)
        if len(query) != VECTOR_DIMENSION or any(
            not math.isfinite(value) for value in query
        ):
            raise ValueError("query vector must contain 1536 finite values")
        vector_text = "[" + ",".join(repr(value) for value in query) + "]"

        try:
            with self.transactions.operation() as connection:
                models = _all(
                    connection,
                    _MODEL_SQL,
                    (MODEL_PROVIDER, MODEL_NAME, MODEL_VERSION, VECTOR_DIMENSION),
                )
                if len(models) != 1:
                    raise SearchUnavailable(
                        "exactly one active embedding model is required"
                    )
                model_id = int(models[0][0])
                _run(connection, "SET LOCAL hnsw.iterative_scan = 'strict_order'")
                rows = _all(
                    connection,
                    _SEARCH_SQL.format(model_id=model_id),
                    (vector_text, allowed, vector_text, limit),
                )
        except SearchUnavailable:
            raise
        except Exception:
            raise SearchUnavailable("PostgreSQL vector search failed") from None

        hits: list[SearchHit] = []
        for row in rows:
            distance = float(row[7])
            if not math.isfinite(distance):
                raise SearchUnavailable("PostgreSQL vector search returned invalid data")
            hits.append(
                SearchHit(
                    chunk_search_id(int(row[1]), int(row[2])),
                    document_search_id(int(row[3])),
                    str(row[4]),
                    max(-1.0, min(1.0, 1.0 - distance)),
                    {
                        "documentVersionId": int(row[1]),
                        "chunkIndex": int(row[2]),
                        "pageNumber": row[5],
                        "sectionTitle": row[6],
                    },
                )
            )
        return tuple(hits)
