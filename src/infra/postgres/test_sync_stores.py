from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

from src.infra.postgres.mcp_store import PostgresMcpTokenStore
from src.infra.postgres.search_history_store import PostgresSearchHistoryStore
from src.infra.postgres.sync_store import PostgresSyncStore
from src.shared.mcp import McpTokenRecord
from src.shared.search import (
    SearchAnswerHistoryRecord,
    SearchCitationHistoryRecord,
    SearchHistoryBundle,
    SearchHistoryRecord,
)
from src.shared.sync import SyncDeliveryAttemptRow, SyncEventRow


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
EVENT_ID = "10000000-0000-4000-8000-000000000001"
TOKEN_ID = "20000000-0000-4000-8000-000000000002"
CHUNK_ID = "30000000-0000-4000-8000-000000000003"
DOCUMENT_ID = "40000000-0000-4000-8000-000000000004"


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rows: list[object] = []
        self.rowcount = 0
        self.closed = False

    def _run(self, sql: str, parameters: object, many: bool) -> None:
        self.connection.calls.append((sql, parameters, many))
        rows, rowcount = (
            self.connection.plans.pop(0)
            if self.connection.plans
            else ([], 1)
        )
        self.rows = list(rows)
        self.rowcount = rowcount

    def execute(self, sql: str, parameters: object = None) -> None:
        self._run(sql, parameters, False)

    def executemany(self, sql: str, parameters: object) -> None:
        self._run(sql, parameters, True)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self) -> None:
        self.plans: list[tuple[list[object], int]] = []
        self.calls: list[tuple[str, object, bool]] = []
        self.cursors: list[_Cursor] = []

    def cursor(self) -> _Cursor:
        cursor = _Cursor(self)
        self.cursors.append(cursor)
        return cursor


class _Manager:
    def __init__(self) -> None:
        self.connection = _Connection()
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


def _event_row(*, status: str = "PENDING") -> SyncEventRow:
    return SyncEventRow(
        EVENT_ID,
        "DOCUMENT:7:DOCUMENT_DELETED",
        "DOCUMENT",
        7,
        None,
        "DOCUMENT_DELETED",
        {"documentId": 7, "nested": {"b": 2, "a": 1}},
        '{"documentId":7,"nested":{"a":1,"b":2}}',
        status,
        NOW,
        NOW,
        5,
    )


def _event_database_row(event: SyncEventRow) -> tuple[object, ...]:
    return (
        event.id,
        event.idempotency_key,
        event.aggregate_type,
        str(event.aggregate_id),
        event.aggregate_version,
        event.event_type,
        json.dumps(event.payload),
        event.status,
        event.occurred_at,
        event.available_at,
        event.max_retries,
        event.failure_count,
        event.owner_name,
        event.claim_token,
        event.locked_at,
        event.lease_expires_at,
        event.processed_at,
        event.failed_at,
        event.error_type,
        event.error_message,
    )


class PostgresSyncStoreTests(unittest.TestCase):
    def test_IT_SYNC_STORE_001_insert_uses_canonical_json_and_idempotency_conflict(self):
        manager = _Manager()
        store = PostgresSyncStore(manager)  # type: ignore[arg-type]
        event = _event_row()
        manager.connection.plans.extend([
            ([(EVENT_ID,)], 1),
            ([], 0),
        ])

        self.assertTrue(store.insert_event(event))
        self.assertFalse(store.insert_event(event))

        sql, parameters, _ = manager.connection.calls[0]
        self.assertIn("%s::jsonb", sql)
        self.assertIn("ON CONFLICT (idempotency_key) DO NOTHING", sql)
        self.assertEqual(event.canonical_payload, parameters[6])

    def test_IT_SYNC_STORE_002_get_uses_row_lock_skip_locked_and_restores_identifier(self):
        manager = _Manager()
        store = PostgresSyncStore(manager)  # type: ignore[arg-type]
        manager.connection.plans.append(([ _event_database_row(_event_row()) ], 1))

        loaded = store.get_event(EVENT_ID, for_update=True, skip_locked=True)

        self.assertIsNotNone(loaded)
        self.assertEqual(7, loaded.aggregate_id)
        self.assertEqual({"a": 1, "b": 2}, loaded.payload["nested"])
        self.assertRegex(
            " ".join(manager.connection.calls[0][0].split()),
            r"FOR UPDATE SKIP LOCKED$",
        )

    def test_IT_SYNC_STORE_003_save_and_attempt_number_are_explicit(self):
        manager = _Manager()
        store = PostgresSyncStore(manager)  # type: ignore[arg-type]
        manager.connection.plans.extend([([], 1), ([], 1)])
        event = replace(_event_row(), status="FAILED", available_at=None)
        attempt = SyncDeliveryAttemptRow(
            "50000000-0000-4000-8000-000000000005",
            EVENT_ID,
            4,
            "STARTED",
            NOW,
        )

        store.save_event(event)
        store.insert_attempt(attempt)

        update_sql = " ".join(manager.connection.calls[0][0].split())
        attempt_sql, attempt_values, _ = manager.connection.calls[1]
        self.assertIn("UPDATE sync_events SET", update_sql)
        self.assertIn("attempt_no", attempt_sql)
        self.assertEqual(4, attempt_values[2])
        self.assertNotRegex(attempt_sql.casefold(), r"serial|sequence|identity")

    def test_IT_SYNC_STORE_004_numeric_string_identifier_round_trips(self):
        manager = _Manager()
        store = PostgresSyncStore(manager)  # type: ignore[arg-type]
        event = replace(_event_row(), aggregate_id="007")
        manager.connection.plans.append(([(EVENT_ID,)], 1))

        self.assertTrue(store.insert_event(event))
        self.assertEqual('"007"', manager.connection.calls[0][1][3])

        database_row = list(_event_database_row(event))
        database_row[3] = '"007"'
        manager.connection.plans.append(([tuple(database_row)], 1))
        loaded = store.get_event(EVENT_ID)
        self.assertEqual("007", loaded.aggregate_id)


class PostgresMcpStoreTests(unittest.TestCase):
    def test_IT_MCP_001_invalid_uuid_get_does_not_query_database(self):
        manager = _Manager()
        store = PostgresMcpTokenStore(manager)  # type: ignore[arg-type]

        self.assertIsNone(store.get("not-a-uuid"))
        self.assertEqual((0, []), (manager.operations, manager.connection.calls))

    def test_IT_MCP_002_update_keeps_first_revoke_and_touch_is_atomic(self):
        manager = _Manager()
        store = PostgresMcpTokenStore(manager)  # type: ignore[arg-type]
        record = McpTokenRecord(TOKEN_ID, 7, "a" * 64, NOW, revoked_at=NOW)
        manager.connection.plans.extend([
            ([(TOKEN_ID, 7, "a" * 64, NOW, None, NOW)], 1),
            ([(TOKEN_ID,)], 1),
        ])

        stored = store.update(record)
        touched = store.touch_last_used_if_active(TOKEN_ID, "a" * 64, NOW)

        self.assertEqual(NOW, stored.revoked_at)
        self.assertTrue(touched)
        update_sql = " ".join(manager.connection.calls[0][0].split())
        touch_sql = " ".join(manager.connection.calls[1][0].split())
        self.assertIn(
            "revoked_at = COALESCE( revoked_at, "
            "GREATEST(%s, last_used_at, created_at) )",
            update_sql,
        )
        self.assertIn(
            "last_used_at = GREATEST( last_used_at, created_at, %s )",
            touch_sql,
        )
        self.assertIn("revoked_at IS NULL", touch_sql)
        self.assertIn("RETURNING token_id", touch_sql)


class PostgresSearchHistoryStoreTests(unittest.TestCase):
    def test_IT_SEARCH_HISTORY_001_bundle_inserts_in_one_ambient_operation(self):
        manager = _Manager()
        store = PostgresSearchHistoryStore(manager)  # type: ignore[arg-type]
        manager.connection.plans.extend([
            ([(41,)], 1),
            ([], 1),
            ([], 2),
        ])
        bundle = SearchHistoryBundle(
            SearchHistoryRecord("7", "query", NOW, 12.5, 2, "SUCCESS", "b" * 64),
            SearchAnswerHistoryRecord("7", NOW, "answer", "SUCCESS", "b" * 64),
            (
                SearchCitationHistoryRecord("7", NOW, 1, CHUNK_ID, DOCUMENT_ID),
                SearchCitationHistoryRecord("7", NOW, 2, CHUNK_ID, DOCUMENT_ID),
            ),
        )

        store.record(bundle)

        self.assertEqual(1, manager.operations)
        self.assertEqual(3, len(manager.connection.calls))
        self.assertFalse(manager.connection.calls[0][2])
        self.assertTrue(manager.connection.calls[2][2])
        citation_values = manager.connection.calls[2][1]
        self.assertEqual((41, 1, CHUNK_ID, DOCUMENT_ID), citation_values[0])

    def test_IT_SEARCH_HISTORY_002_purge_is_strict_and_ops_reads_only_timestamp(self):
        manager = _Manager()
        store = PostgresSearchHistoryStore(manager)  # type: ignore[arg-type]
        manager.connection.plans.extend([([], 1), ([(NOW,)], 1)])

        store.purge_before(NOW)
        snapshots = store.ops_search_snapshots(NOW)

        purge_sql = " ".join(manager.connection.calls[0][0].split())
        snapshot_sql = " ".join(manager.connection.calls[1][0].split())
        self.assertIn("requested_at < %s", purge_sql)
        self.assertIn("SELECT requested_at", snapshot_sql)
        self.assertNotIn("query_text", snapshot_sql)
        self.assertEqual((NOW,), tuple(item.requested_at for item in snapshots))


if __name__ == "__main__":
    unittest.main()
