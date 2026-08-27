from __future__ import annotations

import unittest

from src.infra.postgres.transaction import PostgresTransactionManager
from src.infra.search import PostgresKeywordSearcher
from src.search.calls import keyword_search
from src.shared import (
    KeywordTransportError,
    SearchUnavailable,
    chunk_search_id,
    document_search_id,
)


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rows = ()

    def execute(self, sql, parameters=()) -> None:
        normalized = " ".join(sql.split())
        self.connection.statements.append((normalized, parameters))
        failure = self.connection.failure
        if failure is not None and normalized.startswith("WITH terms"):
            raise failure
        if normalized.startswith("WITH terms"):
            self.rows = self.connection.rows
        else:
            self.rows = ()

    def fetchall(self):
        return self.rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, rows=(), failure=None) -> None:
        self.statements = []
        self.rows = rows
        self.failure = failure

    def cursor(self):
        return _Cursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _searcher(connection):
    return PostgresKeywordSearcher(PostgresTransactionManager(lambda: connection))


class PostgresKeywordSearcherTests(unittest.TestCase):
    def test_IT_KEYWORD_001_blank_query_or_no_candidates_execute_no_sql(self):
        opened = []

        def connect():
            opened.append(True)
            return _Connection()

        searcher = PostgresKeywordSearcher(PostgresTransactionManager(connect))
        allowed = frozenset({document_search_id(3)})

        self.assertEqual((), searcher.search("연차", frozenset(), 20, timeout_seconds=30.0))
        self.assertEqual((), searcher.search("   ", allowed, 20, timeout_seconds=30.0))
        self.assertEqual((), searcher.search("연차", allowed, 0, timeout_seconds=30.0))
        self.assertEqual([], opened)

    def test_IT_KEYWORD_002_bm25_scores_the_permission_filtered_current_versions(self):
        connection = _Connection(
            rows=((22, 0, 3, "연차 규정 본문", 1, "제1절", 4.25),)
        )

        hits = _searcher(connection).search(
            "연차 규정",
            frozenset({document_search_id(3)}),
            8,
            timeout_seconds=30.0,
        )

        self.assertEqual(1, len(hits))
        self.assertEqual(chunk_search_id(22, 0), hits[0].chunk_id)
        self.assertEqual(document_search_id(3), hits[0].document_id)
        self.assertEqual("연차 규정 본문", hits[0].content)
        self.assertEqual(4.25, hits[0].score)
        self.assertEqual(
            {
                "documentVersionId": 22,
                "chunkIndex": 0,
                "pageNumber": 1,
                "sectionTitle": "제1절",
            },
            hits[0].metadata,
        )

        sql, parameters = next(
            item for item in connection.statements if item[0].startswith("WITH terms")
        )
        self.assertEqual(("연차 규정", [3], 8), parameters)
        self.assertIn("to_tsvector('simple', %s)", sql)
        self.assertIn("c.search_vector @@ t.term_query", sql)
        self.assertIn("d.document_id = ANY(%s::bigint[])", sql)
        self.assertIn("d.current_version_id = v.document_version_id", sql)
        self.assertIn("d.status = 'INDEXED'", sql)
        self.assertIn("d.deleted_at IS NULL", sql)
        self.assertIn("v.status = 'INDEXED'", sql)
        self.assertIn("ORDER BY score DESC, document_version_id, chunk_index", sql)
        self.assertNotIn("ts_rank", sql)

    def test_IT_KEYWORD_003_score_is_raw_bm25_with_lucene_constants(self):
        connection = _Connection(rows=())
        _searcher(connection).search(
            "연차",
            frozenset({document_search_id(3)}),
            8,
            timeout_seconds=30.0,
        )

        sql = next(
            statement
            for statement, _ in connection.statements
            if statement.startswith("WITH terms")
        )
        self.assertIn(
            "ln( 1.0 + (corpus.doc_count - f.doc_frequency + 0.5) "
            "/ (f.doc_frequency + 0.5) )",
            sql,
        )
        self.assertIn("(o.term_frequency * (1.2 + 1.0))", sql)
        self.assertIn("1.2 * (1.0 - 0.75 + 0.75 * o.doc_length / corpus.avg_length)", sql)
        self.assertIn("greatest(c.token_estimate, 1)::float8 AS doc_length", sql)

    def test_IT_KEYWORD_004_request_timeout_bounds_the_statement_and_is_reset(self):
        connection = _Connection(rows=())
        _searcher(connection).search(
            "연차",
            frozenset({document_search_id(3)}),
            8,
            timeout_seconds=30.0,
        )

        issued = [statement for statement, _ in connection.statements]
        self.assertIn("SET LOCAL statement_timeout = 30000", issued)
        self.assertIn("SET LOCAL statement_timeout = DEFAULT", issued)
        self.assertLess(
            issued.index("SET LOCAL statement_timeout = 30000"),
            next(i for i, item in enumerate(issued) if item.startswith("WITH terms")),
        )

        searcher = _searcher(_Connection(rows=()))
        for invalid in (0.0, -1.0, float("inf"), True, "30"):
            with self.assertRaises(ValueError):
                searcher.search(
                    "연차",
                    frozenset({document_search_id(3)}),
                    8,
                    timeout_seconds=invalid,
                )

    def test_IT_KEYWORD_005_transport_failures_stay_retryable(self):
        import psycopg

        allowed = frozenset({document_search_id(3)})
        for failure in (
            _DriverError("08006"),
            _DriverError("57014"),
            _DriverError("53300"),
            psycopg.OperationalError("connection reset"),
            psycopg.InterfaceError("protocol out of sync"),
        ):
            searcher = _searcher(_Connection(failure=failure))
            with self.assertRaises(KeywordTransportError):
                searcher.search("연차", allowed, 8, timeout_seconds=30.0)

        searcher = _searcher(_Connection(failure=ConnectionError("dropped")))
        with self.assertRaises(ConnectionError):
            searcher.search("연차", allowed, 8, timeout_seconds=30.0)

    def test_IT_KEYWORD_006_other_failures_become_search_unavailable(self):
        searcher = _searcher(_Connection(failure=_DriverError("42703")))
        with self.assertRaises(SearchUnavailable) as raised:
            searcher.search(
                "연차",
                frozenset({document_search_id(3)}),
                8,
                timeout_seconds=30.0,
            )
        self.assertEqual("SEARCH_SERVICE_ERROR", raised.exception.code)

        invalid = _searcher(_Connection(rows=((22, 0, 3, "본문", None, None, float("nan")),)))
        with self.assertRaises(SearchUnavailable):
            invalid.search(
                "연차",
                frozenset({document_search_id(3)}),
                8,
                timeout_seconds=30.0,
            )

    def test_IT_KEYWORD_007_S13_app_retries_exhaust_into_one_503(self):
        attempts = []
        delays = []

        class _Flaky:
            def search(self, query, document_ids, limit, *, timeout_seconds):
                attempts.append(timeout_seconds)
                raise KeywordTransportError("transport")

        with self.assertRaises(SearchUnavailable):
            keyword_search(
                _Flaky(),
                "연차",
                frozenset({document_search_id(3)}),
                8,
                sleep=delays.append,
                jitter=lambda low, high: high,
            )

        self.assertEqual([30.0, 30.0, 30.0, 30.0], attempts)
        self.assertEqual([1.0, 2.0, 4.0], delays)

    def test_IT_KEYWORD_008_recovered_transport_failure_returns_hits(self):
        connection = _Connection(rows=((22, 0, 3, "연차 규정 본문", None, None, 2.5),))
        searcher = _searcher(connection)
        calls = []

        class _RecoveringOnce:
            def search(self, query, document_ids, limit, *, timeout_seconds):
                calls.append(query)
                if len(calls) == 1:
                    raise KeywordTransportError("transport")
                return searcher.search(
                    query, document_ids, limit, timeout_seconds=timeout_seconds
                )

        hits = keyword_search(
            _RecoveringOnce(),
            "연차 규정",
            frozenset({document_search_id(3)}),
            8,
            sleep=lambda _seconds: None,
            jitter=lambda low, high: high,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual((chunk_search_id(22, 0),), tuple(hit.chunk_id for hit in hits))

    def test_IT_KEYWORD_009_chunk_ids_match_the_vector_adapter_for_step6_dedup(self):
        from src.infra.search import PostgresVectorSearcher

        class _VectorCursor(_Cursor):
            def execute(self, sql, parameters=()) -> None:
                normalized = " ".join(sql.split())
                self.connection.statements.append((normalized, parameters))
                if normalized.startswith("SELECT embedding_model_id"):
                    self.rows = ((7,),)
                elif normalized.startswith("WITH nearest"):
                    self.rows = ((9, 22, 0, 3, "연차 규정 본문", None, None, 0.2),)
                else:
                    self.rows = ()

        class _VectorConnection(_Connection):
            def cursor(self):
                return _VectorCursor(self)

        vector_connection = _VectorConnection()
        vector_hits = PostgresVectorSearcher(
            PostgresTransactionManager(lambda: vector_connection)
        ).search((1.0,) + (0.0,) * 1535, frozenset({document_search_id(3)}), 8)
        keyword_hits = _searcher(
            _Connection(rows=((22, 0, 3, "연차 규정 본문", None, None, 3.5),))
        ).search(
            "연차 규정",
            frozenset({document_search_id(3)}),
            8,
            timeout_seconds=30.0,
        )

        self.assertEqual(vector_hits[0].chunk_id, keyword_hits[0].chunk_id)
        self.assertEqual(vector_hits[0].document_id, keyword_hits[0].document_id)
        self.assertEqual(vector_hits[0].metadata, keyword_hits[0].metadata)


if __name__ == "__main__":
    unittest.main()
