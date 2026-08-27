from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime

from src.infra.postgres.document_rows import FileObjectRow, IndexJobRow
from src.infra.postgres.document_store import PostgresDocumentStore
from src.shared import StorageLocation


NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, parameters: object = None) -> None:
        self.connection.calls.append((_normalized(sql), parameters))
        self.rows = list(self.connection.script.pop(0))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        self.connection.closed_cursors += 1


class _Connection:
    def __init__(self, script: list[list[tuple[object, ...]]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, object]] = []
        self.closed_cursors = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Transactions:
    def __init__(self, script: list[list[tuple[object, ...]]]) -> None:
        self.connection = _Connection(script)
        self.operations = 0
        self.transactions = 0

    @contextmanager
    def operation(self):
        self.operations += 1
        yield self.connection

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


def _version_row(identifier: int, number: int) -> tuple[object, ...]:
    return (identifier, 8, number, 5, "title", "INDEXED", NOW, NOW)


class PostgresDocumentStoreContractTests(unittest.TestCase):
    def test_IT_DOCUMENT_STORE_001_identity_preallocation_uses_identity_only(self):
        transactions = _Transactions([[(91,)]])
        store = PostgresDocumentStore(transactions)  # type: ignore[arg-type]

        self.assertEqual(91, store.next_id("version"))
        self.assertEqual(0, store.next_id("event"))

        self.assertEqual(1, len(transactions.connection.calls))
        sql, parameters = transactions.connection.calls[0]
        self.assertEqual(
            "SELECT nextval(pg_get_serial_sequence(%s, %s))",
            sql,
        )
        self.assertEqual(
            ("document_versions", "document_version_id"), parameters
        )
        self.assertNotIn("version_no", sql)

    def test_IT_DOCUMENT_STORE_002_locks_then_reads_version_numbers(self):
        transactions = _Transactions([
            [],
            [_version_row(20, 1), _version_row(21, 2), _version_row(22, 3)],
        ])
        store = PostgresDocumentStore(transactions)  # type: ignore[arg-type]

        store.lock_document(8)
        versions = store.versions(8)

        self.assertEqual(4, max(row.version_no for row in versions) + 1)
        lock_sql, _ = transactions.connection.calls[0]
        versions_sql, _ = transactions.connection.calls[1]
        self.assertEqual(
            "SELECT document_id FROM documents "
            "WHERE document_id = %s FOR UPDATE",
            lock_sql,
        )
        self.assertIn("ORDER BY version_no", versions_sql)
        self.assertNotIn("nextval", versions_sql.casefold())

    def test_IT_DOCUMENT_STORE_003_terminal_lock_order_is_job_version_document(self):
        transactions = _Transactions([[], [], []])
        store = PostgresDocumentStore(transactions)  # type: ignore[arg-type]

        with store.transaction():
            store.lock_job(30)
            store.lock_version(20)
            store.lock_document(8)

        statements = [call[0] for call in transactions.connection.calls]
        self.assertEqual(1, transactions.transactions)
        self.assertEqual(
            ["indexing_jobs", "document_versions", "documents"],
            [
                next(
                    table
                    for table in (
                        "indexing_jobs", "document_versions", "documents"
                    )
                    if f"FROM {table}" in sql
                )
                for sql in statements
            ],
        )
        self.assertTrue(all(sql.endswith("FOR UPDATE") for sql in statements))

    def test_IT_DOCUMENT_STORE_004_file_dedupe_and_provider_case_are_explicit(self):
        stored = (
            7, "a" * 64, 3, "a.txt", "text/plain", "TXT",
            "S3", "bucket", "documents/a.txt",
        )
        transactions = _Transactions([[(7,)], [], [stored]])
        store = PostgresDocumentStore(transactions)  # type: ignore[arg-type]
        row = FileObjectRow(
            7,
            "a" * 64,
            3,
            "a.txt",
            "text/plain",
            "TXT",
            StorageLocation("s3", "bucket", "documents/a.txt", 3),
        )

        self.assertTrue(store.insert_file_if_absent(row))
        self.assertFalse(store.insert_file_if_absent(row))
        loaded = store.find_file(row.digest, row.size)

        self.assertEqual("s3", loaded.location.provider)
        insert_sql, parameters = transactions.connection.calls[0]
        self.assertIn(
            "ON CONFLICT (content_sha256, file_size) DO NOTHING", insert_sql
        )
        self.assertIn("RETURNING file_object_id", insert_sql)
        self.assertEqual("S3", parameters[6])

    def test_IT_DOCUMENT_STORE_005_job_registration_time_and_no_shadow_event(self):
        transactions = _Transactions([[]])
        store = PostgresDocumentStore(transactions)  # type: ignore[arg-type]

        store.insert_job(IndexJobRow(30, 20, NOW))
        store.append_event(object())

        self.assertEqual(1, len(transactions.connection.calls))
        sql, parameters = transactions.connection.calls[0]
        self.assertIn("INSERT INTO indexing_jobs", sql)
        self.assertIn("created_at", sql)
        self.assertEqual((30, 20, "PENDING", NOW), parameters)

    def test_IT_DOCUMENT_STORE_006_object_reference_uses_full_location(self):
        transactions = _Transactions([[(1,)]])
        store = PostgresDocumentStore(transactions)  # type: ignore[arg-type]
        location = StorageLocation("s3", "bucket", "documents/key.txt", 3)

        self.assertTrue(store.is_referenced(location))

        sql, parameters = transactions.connection.calls[0]
        self.assertIn("FROM file_objects", sql)
        self.assertIn("storage_provider = %s", sql)
        self.assertIn("storage_namespace = %s", sql)
        self.assertIn("storage_key = %s", sql)
        self.assertEqual(("S3", "bucket", "documents/key.txt"), parameters)


if __name__ == "__main__":
    unittest.main()
