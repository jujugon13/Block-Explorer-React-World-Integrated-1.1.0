from __future__ import annotations

import unittest

from src.infra.postgres.transaction import PostgresTransactionManager
from src.infra.search import PostgresVectorSearcher
from src.shared import document_search_id


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rows = ()

    def execute(self, sql, parameters=()) -> None:
        normalized = " ".join(sql.split())
        self.connection.statements.append((normalized, parameters))
        if normalized.startswith("SELECT embedding_model_id"):
            self.rows = ((7,),)
        elif normalized.startswith("WITH nearest"):
            self.rows = (
                (9, 22, 0, 3, "현재 버전 본문", 1, "제1절", 0.125),
            )
        else:
            self.rows = ()

    def fetchall(self):
        return self.rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.statements = []

    def cursor(self):
        return _Cursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class PostgresVectorSearcherTests(unittest.TestCase):
    def test_IT_SEARCH_001_empty_permission_candidates_execute_no_sql(self):
        opened = []

        def connect():
            opened.append(True)
            return _Connection()

        searcher = PostgresVectorSearcher(PostgresTransactionManager(connect))

        self.assertEqual((), searcher.search((0.0,) * 1536, frozenset(), 20))
        self.assertEqual([], opened)

    def test_IT_SEARCH_002_cosine_hnsw_filters_active_current_allowed_documents(self):
        connection = _Connection()
        searcher = PostgresVectorSearcher(
            PostgresTransactionManager(lambda: connection)
        )

        hits = searcher.search(
            (1.0,) + (0.0,) * 1535,
            frozenset({document_search_id(3)}),
            8,
        )

        self.assertEqual(1, len(hits))
        self.assertEqual(document_search_id(3), hits[0].document_id)
        self.assertEqual(0.875, hits[0].score)
        sql = next(sql for sql, _ in connection.statements if sql.startswith("WITH nearest"))
        self.assertIn("embedding::vector(1536) <=>", sql)
        self.assertIn("dv.embedding_model_id = 7", sql)
        self.assertIn("dv.status = 'ACTIVE'", sql)
        self.assertIn("d.current_version_id = v.document_version_id", sql)
        self.assertIn("d.document_id = ANY(%s::bigint[])", sql)
        self.assertNotIn("ts_rank", sql)
        self.assertTrue(
            any("hnsw.iterative_scan = 'strict_order'" in sql for sql, _ in connection.statements)
        )


if __name__ == "__main__":
    unittest.main()
