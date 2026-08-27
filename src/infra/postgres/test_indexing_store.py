from __future__ import annotations

import unittest
from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from src.infra.postgres.indexing_store import PostgresIndexingStore
from src.infra.postgres.transaction import PostgresTransactionManager


NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.result = None

    def execute(self, sql: str, parameters=()) -> None:
        normalized = _normalized(sql)
        self.connection.statements.append((normalized, tuple(parameters)))
        if normalized.startswith("SET TRANSACTION"):
            self.result = None
        else:
            self.result = (
                self.connection.results.popleft()
                if self.connection.results
                else None
            )

    def executemany(self, sql: str, parameter_rows) -> None:
        self.connection.many.append(
            (_normalized(sql), tuple(tuple(row) for row in parameter_rows))
        )

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result or ()

    def close(self) -> None:
        self.connection.cursor_closes += 1


class _Connection:
    def __init__(self, *results) -> None:
        self.results = deque(results)
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.many: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursor_closes = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


def _job_row(*, status="PENDING", token=None):
    return (
        11,
        22,
        status,
        7,
        3,
        0,
        NOW,
        None,
        5 if token is not None else None,
        token,
        NOW if token is not None else None,
        NOW + timedelta(minutes=5) if token is not None else None,
        NOW if token is not None else None,
        None,
        None,
        None,
        None,
    )


class PostgresIndexingStoreTests(unittest.TestCase):
    @staticmethod
    def store(*results):
        connection = _Connection(*results)
        manager = PostgresTransactionManager(lambda: connection)
        return PostgresIndexingStore(manager), connection

    def test_IT_INDEXING_STORE_001_claim_uses_skip_locked_and_fixed_order(self):
        store, connection = self.store(_job_row())

        with store.transaction():
            job = store.lock_next_pending_job(NOW)

        self.assertEqual((11, 7, "PENDING"), (job.id, job.priority, job.status))
        sql, parameters = next(
            item for item in connection.statements if "SKIP LOCKED" in item[0]
        )
        self.assertIn(
            "ORDER BY priority DESC, created_at ASC, job_id ASC ",
            sql + " ",
        )
        self.assertIn("FOR UPDATE SKIP LOCKED LIMIT 1", sql)
        self.assertEqual((NOW,), parameters)

    def test_IT_INDEXING_STORE_002_recovery_snapshot_does_not_lock(self):
        store, connection = self.store([(4,), (9,)])

        with store.read():
            result = store.expired_job_ids(NOW, 100)

        self.assertEqual((4, 9), result)
        sql, parameters = next(
            item
            for item in connection.statements
            if item[0].startswith("SELECT job_id FROM indexing_jobs")
        )
        self.assertNotIn("FOR UPDATE", sql)
        self.assertNotIn("SKIP LOCKED", sql)
        self.assertIn("ORDER BY lease_expires_at ASC, job_id ASC LIMIT %s", sql)
        self.assertEqual((NOW, 100), parameters)

    def test_IT_INDEXING_STORE_003_job_mapping_and_save_are_separate(self):
        token = UUID("00000000-0000-0000-0000-000000000123")
        store, connection = self.store(_job_row(status="PROCESSING", token=token))

        with store.transaction():
            job = store.lock_job(11)
            self.assertEqual(str(token), job.claim_token)
            job.lease_expires_at = NOW + timedelta(minutes=6)
            store.save_job(job)

        update_sql, parameters = next(
            item
            for item in connection.statements
            if item[0].startswith("UPDATE indexing_jobs")
        )
        self.assertNotIn("FOR UPDATE", update_sql)
        self.assertEqual(11, parameters[-1])
        self.assertEqual(NOW + timedelta(minutes=6), parameters[8])

    def test_IT_INDEXING_STORE_004_document_insert_never_creates_placeholder(self):
        store, connection = self.store(None)
        document = SimpleNamespace(
            id=3,
            status="UPLOADED",
            current_version_id=None,
            latest_version_id=8,
            deleted_at=None,
        )

        with self.assertRaisesRegex(RuntimeError, "created by the document store"):
            with store.transaction():
                store.insert_document(document)

        business_sql = " ".join(sql for sql, _ in connection.statements)
        self.assertNotIn("INSERT INTO documents", business_sql)

    def test_IT_INDEXING_STORE_005_vector_text_mapping_needs_no_pgvector_package(self):
        store, connection = self.store([(7, 22, 0, 2, "[1.25,-2,3]", "ACTIVE")])

        with store.read():
            vector = store.list_vectors()[0]
        with store.transaction():
            vector.status = "STALE"
            store.save_vector(vector)

        self.assertEqual((1.25, -2.0, 3.0), vector.values)
        select_sql = next(
            sql for sql, _ in connection.statements if "FROM document_vectors" in sql
        )
        update_sql, parameters = next(
            item
            for item in connection.statements
            if item[0].startswith("UPDATE document_vectors")
        )
        self.assertIn("embedding::text AS embedding_text", select_sql)
        self.assertIn("embedding = %s::vector", update_sql)
        self.assertEqual(("[1.25,-2.0,3.0]", "STALE", 7), parameters)

    def test_IT_INDEXING_STORE_006_worker_registration_uses_xact_advisory_lock(self):
        store, connection = self.store()

        with store.transaction():
            store.lock_worker_registration("instance-one")

        sql, parameters = next(
            item
            for item in connection.statements
            if "pg_advisory_xact_lock" in item[0]
        )
        self.assertIn("hashtextextended(%s, 0)", sql)
        self.assertEqual(("instance-one",), parameters)

    def test_IT_INDEXING_STORE_007_requested_id_advances_identity(self):
        store, connection = self.store(None, (1,), (2,), (5,))

        with store.transaction():
            identifier = store.next_id("model", 5)

        self.assertEqual(5, identifier)
        nextval_calls = [
            (sql, parameters)
            for sql, parameters in connection.statements
            if sql.startswith("SELECT nextval")
        ]
        self.assertEqual(3, len(nextval_calls))
        self.assertTrue(
            all(
                parameters == ("embedding_models", "embedding_model_id")
                for _sql, parameters in nextval_calls
            )
        )

    def test_IT_INDEXING_STORE_008_model_registry_uses_provider_and_version_columns(self):
        row = (
            2,
            "text-embedding-3-small",
            1536,
            True,
            True,
            "OPENAI",
            "text-embedding-3-small",
        )
        store, connection = self.store([row])

        with store.read():
            model = store.list_models()[0]
        with store.transaction():
            store.save_model(model)

        select_sql = next(
            sql for sql, _ in connection.statements if "FROM embedding_models" in sql
        )
        update_sql, parameters = next(
            item
            for item in connection.statements
            if item[0].startswith("UPDATE embedding_models")
        )
        self.assertIn("model_name AS name", select_sql)
        self.assertIn("provider = %s, model_version = %s", update_sql)
        self.assertEqual(
            (
                "text-embedding-3-small",
                1536,
                True,
                True,
                "OPENAI",
                "text-embedding-3-small",
                2,
            ),
            parameters,
        )


if __name__ == "__main__":
    unittest.main()
