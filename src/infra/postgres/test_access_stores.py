from __future__ import annotations

import unittest
from datetime import UTC, datetime

from src.infra.postgres.collection_store import PostgresCollectionStore
from src.infra.postgres.permission_store import PostgresPermissionStore
from src.infra.postgres.transaction import PostgresTransactionManager
from src.shared.access import CachedPermissionGrant


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, parameters: object = None) -> None:
        normalized = " ".join(sql.lower().split())
        self.connection.statements.append((normalized, parameters))
        expects_rows = normalized.startswith("select ") or " returning " in normalized
        if expects_rows and not normalized.startswith("set transaction"):
            if not self.connection.responses:
                raise AssertionError("fake SQL response was not configured")
            self.rows = self.connection.responses.pop(0)
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        self.connection.cursor_closes += 1


class _Connection:
    def __init__(self, responses: list[list[tuple[object, ...]]]) -> None:
        self.responses = list(responses)
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursor_closes = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


class AccessStoreSqlTests(unittest.TestCase):
    def manager(self, connection: _Connection) -> PostgresTransactionManager:
        return PostgresTransactionManager(lambda: connection)

    def test_IT_ACCESS_001_collection_lock_order_records_and_mapping_crud(self):
        collection_row = (10, "root", 1, None, "PRIVATE", "ACTIVE")
        connection = _Connection([
            [(8,)],
            [(2,)],
            [(5,)],
            [collection_row],
            [collection_row],
            [collection_row, (11, "child", 1, 10, "PUBLIC", "ACTIVE")],
            [(False,)],
            [(10,), (11,)],
            [(44,), (45,)],
            [(44,), (45,), (46,)],
        ])
        manager = self.manager(connection)
        store = PostgresCollectionStore(manager)

        with manager.transaction():
            store.lock_collections((8, 2, 5))
            created = store.create_collection("root", 1, None, "PRIVATE")
            fetched = store.collection(10)
            listed = store.collections()
            self.assertFalse(store.has_mapping(10, 44))
            store.add_mapping(10, 44)
            store.remove_mapping(10, 44)
            store.remove_mappings((10, 11))
            store.remove_mappings(())
            store.set_visibility(10, "PUBLIC")
            store.set_status(10, "DELETED")
            collection_ids = store.collection_ids_for_document(44)
            document_ids = store.document_ids_in_collection(10)
            subtree_documents = store.document_ids_in_collections((10, 11))

        self.assertEqual((10, "root", 1, None, "PRIVATE", "ACTIVE"), (
            created.id,
            created.name,
            created.owner_user_id,
            created.parent_id,
            created.visibility,
            created.status,
        ))
        self.assertEqual(created, fetched)
        self.assertEqual((10, 11), tuple(item.id for item in listed))
        self.assertEqual(frozenset({10, 11}), collection_ids)
        self.assertEqual(frozenset({44, 45}), document_ids)
        self.assertEqual(frozenset({44, 45, 46}), subtree_documents)

        lock_parameters = [
            parameters
            for sql, parameters in connection.statements
            if "from collections" in sql and "for update" in sql
        ]
        self.assertEqual([(8,), (2,), (5,)], lock_parameters)
        self.assertTrue(any(
            sql.startswith("insert into collection_documents")
            for sql, _ in connection.statements
        ))
        self.assertTrue(any(
            sql.startswith("delete from collection_documents")
            and "any(%s)" in sql
            for sql, _ in connection.statements
        ))
        self.assertEqual((1, 0, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))
        self.assertEqual([], connection.responses)

    def test_IT_ACCESS_002_permission_resource_rows_and_lock_sequence(self):
        now = datetime(2026, 8, 27, tzinfo=UTC)
        collection_permission = (
            8, "READ", "USER", None, 5, 2, None, None, now
        )
        document_permission = (
            7, "WRITE", "ROLE", 44, None, None, None, "EDITOR", None
        )
        connection = _Connection([
            [(44,)],
            [(7,)],
            [collection_permission],
            [document_permission],
            [document_permission, collection_permission],
            [collection_permission],
            [document_permission, collection_permission],
        ])
        manager = self.manager(connection)
        store = PostgresPermissionStore(manager)

        with manager.transaction():
            store.lock_resource("DOCUMENT", 44)
            store.lock_permission(7)
            created = store.create_permission(
                "COLLECTION", 5, "READ", "USER", 2, None, None, now
            )
            fetched = store.permission(7)
            all_rows = store.all_permissions()
            collection_rows = store.permissions_for_resource("COLLECTION", 5)
            document_rows = store.permissions_for_document(44, (5,))
            store.delete_permission(7)

        self.assertEqual(("COLLECTION", 5, 2), (
            created.resource_kind,
            created.resource_id,
            created.user_id,
        ))
        self.assertEqual(("DOCUMENT", 44, "EDITOR"), (
            fetched.resource_kind,
            fetched.resource_id,
            fetched.role_code,
        ))
        self.assertEqual((7, 8), tuple(item.permission_id for item in all_rows))
        self.assertEqual((8,), tuple(item.permission_id for item in collection_rows))
        self.assertEqual((7, 8), tuple(item.permission_id for item in document_rows))

        statements = [sql for sql, _ in connection.statements]
        resource_lock = next(i for i, sql in enumerate(statements) if (
            "from documents" in sql and "for update" in sql
        ))
        permission_lock = next(i for i, sql in enumerate(statements) if (
            "from direct_permissions" in sql and "for update" in sql
        ))
        insert = next(i for i, sql in enumerate(statements) if (
            sql.startswith("insert into direct_permissions")
        ))
        self.assertLess(resource_lock, permission_lock)
        self.assertLess(permission_lock, insert)
        insert_parameters = connection.statements[insert][1]
        self.assertEqual((None, 5), insert_parameters[2:4])
        self.assertEqual((1, 0, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))
        self.assertEqual([], connection.responses)

    def test_IT_ACCESS_003_user_cache_joins_ledger_and_replacement_is_idempotent(self):
        expiry = datetime(2026, 8, 28, tzinfo=UTC)
        connection = _Connection([
            [[7, "READ", None], [9, "WRITE", expiry]],
            [],
            [(True,)],
        ])
        manager = self.manager(connection)
        store = PostgresPermissionStore(manager)
        grants = (
            CachedPermissionGrant(7, "READ", None),
            CachedPermissionGrant(9, "WRITE", expiry),
        )

        with manager.transaction():
            cached = store.cached_grants(2, 44)
            missing = store.cached_grants(3, 44)
            store.put_cached_grant(2, 44, grants[0])
            store.replace_cached_grants(2, 44, grants)
            store.invalidate_permission_cache(7)
            store.invalidate_document_cache(44)
            has_cache = store.has_cached_grants()

        self.assertEqual(grants, cached)
        self.assertIsNone(missing)
        self.assertTrue(has_cache)
        cache_reads = [
            (sql, parameters)
            for sql, parameters in connection.statements
            if sql.startswith("select p.permission_id")
        ]
        self.assertEqual(2, len(cache_reads))
        self.assertTrue(all(
            "join direct_permissions" in sql
            and "p.target_type = 'user'" in sql
            and parameters[1] == 44
            for sql, parameters in cache_reads
        ))
        cache_inserts = [
            (sql, parameters)
            for sql, parameters in connection.statements
            if sql.startswith("insert into document_permission_cache")
        ]
        self.assertEqual(3, len(cache_inserts))
        self.assertTrue(all(
            "on conflict (permission_id, document_id) do nothing" in sql
            and "target_type = 'user'" in sql
            and parameters[2] == 2
            for sql, parameters in cache_inserts
        ))
        replacement_deletes = [
            parameters
            for sql, parameters in connection.statements
            if sql.startswith("delete from document_permission_cache as c")
        ]
        self.assertEqual([(2, 44)], replacement_deletes)
        self.assertEqual((1, 0, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))
        self.assertEqual([], connection.responses)


if __name__ == "__main__":
    unittest.main()
