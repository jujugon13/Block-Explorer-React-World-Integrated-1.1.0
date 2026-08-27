"""PostgreSQL full-text keyword search scored with Okapi BM25."""

from __future__ import annotations

import math
from typing import Any

from src.shared import (
    Identifier,
    KeywordTransportError,
    SearchHit,
    SearchUnavailable,
    chunk_search_id,
    document_search_id,
    resolve_document_search_id,
)


TEXT_SEARCH_CONFIGURATION = "simple"
BM25_K1 = 1.2
BM25_B = 0.75
MAX_STATEMENT_TIMEOUT_MS = 600_000

# 08* 는 연결 예외 계열이다. 나머지는 취소·종료·접속 불가로, S13 이 앱 재시도를
# 허용한 "연결·타임아웃·프로토콜" 범주에 해당한다.
_TRANSPORT_SQLSTATES = frozenset({"53300", "57014", "57P01", "57P02", "57P03"})

# BM25 통계(N·평균 길이·문서 빈도)는 4.5단계 권한 사전 필터가 통과시킨 색인 완료
# 청크 집합 하나만을 모집단으로 삼는다. 모집단을 나누면 IDF 와 점수 규모가 어긋난다.
_SEARCH_SQL = """
WITH terms AS (
    SELECT DISTINCT t.lexeme AS lexeme,
           plainto_tsquery('{configuration}', t.lexeme) AS term_query
      FROM unnest(to_tsvector('{configuration}', %s))
        AS t(lexeme, positions, weights)
),
corpus_chunk AS NOT MATERIALIZED (
    SELECT c.document_version_id,
           c.chunk_index,
           c.content,
           c.page_number,
           c.section_title,
           c.search_vector,
           greatest(c.token_estimate, 1)::float8 AS doc_length,
           d.document_id
      FROM document_chunks c
      JOIN document_versions v
        ON v.document_version_id = c.document_version_id
      JOIN documents d
        ON d.document_id = v.document_id
       AND d.current_version_id = v.document_version_id
     WHERE d.document_id = ANY(%s::bigint[])
       AND d.status = 'INDEXED'
       AND d.deleted_at IS NULL
       AND v.status = 'INDEXED'
),
corpus AS (
    SELECT greatest(count(*), 1)::float8 AS doc_count,
           greatest(coalesce(avg(doc_length), 1.0), 1.0)::float8 AS avg_length
      FROM corpus_chunk
),
occurrence AS (
    SELECT c.document_version_id,
           c.chunk_index,
           c.document_id,
           c.content,
           c.page_number,
           c.section_title,
           c.doc_length,
           t.lexeme,
           lexeme_hit.term_frequency
      FROM terms t
      JOIN corpus_chunk c
        ON c.search_vector @@ t.term_query
     CROSS JOIN LATERAL (
          SELECT greatest(coalesce(array_length(u.positions, 1), 1), 1)::float8
                     AS term_frequency
            FROM unnest(c.search_vector) AS u(lexeme, positions, weights)
           WHERE u.lexeme = t.lexeme
           LIMIT 1
     ) AS lexeme_hit
),
document_frequency AS (
    SELECT lexeme, count(*)::float8 AS doc_frequency
      FROM occurrence
     GROUP BY lexeme
),
scored AS (
    SELECT o.document_version_id,
           o.chunk_index,
           o.document_id,
           o.content,
           o.page_number,
           o.section_title,
           sum(
               ln(
                   1.0
                   + (corpus.doc_count - f.doc_frequency + 0.5)
                   / (f.doc_frequency + 0.5)
               )
               * (o.term_frequency * ({k1} + 1.0))
               / (
                   o.term_frequency
                   + {k1} * (1.0 - {b} + {b} * o.doc_length / corpus.avg_length)
               )
           )::float8 AS score
      FROM occurrence o
      JOIN document_frequency f
        ON f.lexeme = o.lexeme
     CROSS JOIN corpus
     GROUP BY o.document_version_id, o.chunk_index, o.document_id, o.content,
              o.page_number, o.section_title
)
SELECT document_version_id, chunk_index, document_id, content,
       page_number, section_title, score
  FROM scored
 WHERE score > 0
 ORDER BY score DESC, document_version_id, chunk_index
 LIMIT %s
"""

_STATEMENT = _SEARCH_SQL.format(
    configuration=TEXT_SEARCH_CONFIGURATION,
    k1=BM25_K1,
    b=BM25_B,
)


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


def _driver_transport_error(error: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))


def _transport_failure(error: BaseException) -> bool:
    """Classify only connection, timeout, and protocol failures as retryable."""

    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and (
        sqlstate.startswith("08") or sqlstate in _TRANSPORT_SQLSTATES
    ):
        return True
    return _driver_transport_error(error)


def _statement_timeout_ms(timeout_seconds: float) -> int:
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise ValueError("keyword search timeout must be numeric")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("keyword search timeout must be positive")
    return max(1, min(int(timeout_seconds * 1000), MAX_STATEMENT_TIMEOUT_MS))


class PostgresKeywordSearcher:
    """Serve BM25 keyword hits without opening a second connection boundary."""

    def __init__(self, transactions: Any) -> None:
        self.transactions = transactions

    def search(
        self,
        query: str,
        document_ids: frozenset[Identifier],
        limit: int,
        *,
        timeout_seconds: float,
    ) -> tuple[SearchHit, ...]:
        if not isinstance(query, str):
            raise ValueError("keyword query must be text")
        timeout_ms = _statement_timeout_ms(timeout_seconds)
        if isinstance(limit, bool) or limit < 1 or not query.strip():
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

        try:
            with self.transactions.operation() as connection:
                _run(connection, f"SET LOCAL statement_timeout = {timeout_ms}")
                rows = _all(connection, _STATEMENT, (query, allowed, limit))
                _run(connection, "SET LOCAL statement_timeout = DEFAULT")
        except SearchUnavailable:
            raise
        except (ConnectionError, TimeoutError):
            raise
        except Exception as error:
            if _transport_failure(error):
                raise KeywordTransportError(
                    "PostgreSQL keyword search transport failed"
                ) from None
            raise SearchUnavailable("PostgreSQL keyword search failed") from None

        hits: list[SearchHit] = []
        for row in rows:
            score = float(row[6])
            if not math.isfinite(score):
                raise SearchUnavailable(
                    "PostgreSQL keyword search returned invalid data"
                )
            hits.append(
                SearchHit(
                    chunk_search_id(int(row[0]), int(row[1])),
                    document_search_id(int(row[2])),
                    str(row[3]),
                    score,
                    {
                        "documentVersionId": int(row[0]),
                        "chunkIndex": int(row[1]),
                        "pageNumber": row[4],
                        "sectionTitle": row[5],
                    },
                )
            )
        return tuple(hits)
