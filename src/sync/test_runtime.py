from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.indexing import IndexingService
from src.permissions import PermissionService
from src.platform import PlatformApp
from src.shared import Principal, PublicError, Request
from src.sync import (
    HANDLER_FAILURE_MESSAGE,
    HANDLER_FAILURE_TYPE,
    SyncDispatcher,
    SyncHandlerRegistry,
    SyncService,
    indexing_handlers,
    register_sync_routes,
)


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
ADMIN = Principal("admin", frozenset({"ADMIN"}), user_id=1)


class _Failure:
    def commit(self, event, mark_processed):
        raise OSError("provider password=secret")


class SyncRuntimeTests(unittest.TestCase):
    def assert_code(self, expected: str, call) -> None:
        with self.assertRaises(PublicError) as caught:
            call()
        self.assertEqual(expected, caught.exception.code)

    @staticmethod
    def deleted_event(service: SyncService, document_id: int = 7):
        return service.publish_document_deleted(
            document_id,
            payload={"documentId": document_id},
            occurred_at=NOW,
        )

    def test_AC_SYNC_001_transaction_context_rolls_back_outbox_write(self):
        service = SyncService(clock=lambda: NOW)
        with self.assertRaises(RuntimeError):
            with service.transaction():
                self.deleted_event(service)
                raise RuntimeError("domain transaction rolled back")
        self.assertEqual((), service.events())

    def test_AC_SYNC_002_permission_refresh_uses_confirmed_idempotency_key(self):
        service = SyncService(clock=lambda: NOW)
        payload = {
            "source": "DIRECT_DOCUMENT_PERMISSION",
            "permissionId": 9,
            "action": "GRANT",
        }
        first = service.publish_permission_cache_refresh(
            "DIRECT_DOCUMENT_PERMISSION",
            9,
            "GRANT",
            payload=payload,
            occurred_at=NOW,
        )
        replay = service.publish_permission_cache_refresh(
            "DIRECT_DOCUMENT_PERMISSION",
            9,
            "GRANT",
            payload=dict(reversed(tuple(payload.items()))),
            occurred_at=NOW,
        )
        self.assertEqual(first.id, replay.id)
        self.assertEqual(
            "PERMISSION:DIRECT_DOCUMENT_PERMISSION:9:PERMISSION_CACHE_REFRESH_REQUESTED:GRANT",
            first.idempotency_key,
        )
        self.assertEqual(
            ("PERMISSION", 9, "PERMISSION_CACHE_REFRESH_REQUESTED"),
            (first.aggregate_type, first.aggregate_id, first.event_type),
        )

    def test_AC_SYNC_002_permission_service_publishes_grant_and_revoke(self):
        workspace = DocumentWorkspace(MemoryStorage(), clock=lambda: NOW)
        owner = Principal("owner", user_id=7)
        uploaded = workspace.upload(
            owner,
            UploadFile(b"text", "note.txt", "text/plain"),
            title="Title",
            description=None,
            visibility="PRIVATE",
        )
        service = SyncService(clock=lambda: NOW)
        permissions = PermissionService(
            workspace,
            sync_outbox=service,
            clock=lambda: NOW,
        )
        granted = permissions.grant(
            owner,
            "DOCUMENT",
            uploaded.data["documentId"],
            "READ",
            target_type="USER",
            user_id=8,
        )
        permissions.revoke(
            owner,
            "DOCUMENT",
            uploaded.data["documentId"],
            granted.permission_id,
        )
        events = service.events(event_type="PERMISSION_CACHE_REFRESH_REQUESTED")
        self.assertEqual(2, len(events))
        self.assertEqual(
            {"GRANT", "REVOKE"},
            {event.payload["action"] for event in events},
        )

    def test_AC_SYNC_001_admin_summary_events_and_query_validation(self):
        service = SyncService(clock=lambda: NOW)
        self.deleted_event(service)
        app = PlatformApp(principal_resolver=lambda request: ADMIN)
        register_sync_routes(app, service)

        summary = json.loads(app.handle(Request("GET", "/admin/sync/summary")).body)
        events = json.loads(app.handle(Request("GET", "/admin/sync/events")).body)
        self.assertEqual(1, summary["data"]["pending"])
        self.assertEqual("DOCUMENT_DELETED", events["data"]["content"][0]["eventType"])

        filtered = app.handle(
            Request(
                "GET",
                "/admin/sync/events?eventType=DOCUMENT_DELETED&page=0&size=1",
            )
        )
        invalid = app.handle(
            Request("GET", "/admin/sync/events?status=UNKNOWN")
        )
        self.assertEqual(1, json.loads(filtered.body)["data"]["totalElements"])
        self.assertEqual(
            (400, "COMMON-002"),
            (invalid.status, json.loads(invalid.body)["code"]),
        )

    def test_AC_SYS_007_sync_reports_only_first_body_violation(self):
        app = PlatformApp(principal_resolver=lambda request: ADMIN)
        register_sync_routes(app, SyncService(clock=lambda: NOW))
        response = app.handle(
            Request(
                "POST",
                "/admin/sync/reconcile",
                {"Content-Type": "application/json"},
                b'{"cursor":7,"mode":8}',
            )
        )

        body = json.loads(response.body)
        self.assertEqual((400, "COMMON-002"), (response.status, body["code"]))
        self.assertEqual("cursor: 문자열 또는 null이어야 합니다.", body["message"])
        self.assertNotIn("mode", body["message"])

    def test_AC_SYNC_004_product_handler_stales_real_indexing_vectors(self):
        service = SyncService(clock=lambda: NOW, dispatcher_enabled=True)
        workspace = DocumentWorkspace(
            MemoryStorage(),
            sync_outbox=service,
            clock=lambda: NOW,
        )
        owner = Principal("owner", user_id=7)
        uploaded = workspace.upload(
            owner,
            UploadFile(b"text", "note.txt", "text/plain"),
            title="Title",
            description=None,
            visibility="PRIVATE",
        )

        indexing = IndexingService(clock=lambda: NOW)
        document = indexing.add_document(document_id=uploaded.data["documentId"])
        version = indexing.add_version(
            document.id,
            1,
            version_id=uploaded.data["documentVersionId"],
        )
        model = indexing.add_model(dimension=3)
        indexing.put_vectors(version.id, ((1.0, 2.0, 3.0),), model_id=model.id)

        dispatcher = SyncDispatcher(
            service,
            indexing_handlers(indexing, workspace),
        )
        version_event = dispatcher.tick()
        workspace.delete(owner, document.id)
        deletion_event = dispatcher.tick()

        self.assertEqual("PROCESSED", version_event.status)
        self.assertEqual("PROCESSED", deletion_event.status)
        self.assertEqual(1, len(indexing.state.jobs))
        self.assertEqual(("STALE",), tuple(row.status for row in indexing.state.vectors))

    def test_AC_SYNC_004_expired_completion_rolls_back_vector_effect(self):
        clock = [NOW]
        service = SyncService(
            clock=lambda: clock[0],
            dispatcher_enabled=True,
            lease_duration=timedelta(seconds=30),
        )
        workspace = DocumentWorkspace(
            MemoryStorage(),
            sync_outbox=service,
            clock=lambda: clock[0],
        )
        owner = Principal("owner", user_id=7)
        uploaded = workspace.upload(
            owner,
            UploadFile(b"text", "note.txt", "text/plain"),
            title="Title",
            description=None,
            visibility="PRIVATE",
        )
        indexing = IndexingService(clock=lambda: clock[0])
        document = indexing.add_document(document_id=uploaded.data["documentId"])
        version = indexing.add_version(
            document.id,
            1,
            version_id=uploaded.data["documentVersionId"],
        )
        model = indexing.add_model(dimension=3)
        indexing.put_vectors(version.id, ((1.0, 2.0, 3.0),), model_id=model.id)

        SyncDispatcher(
            service,
            indexing_handlers(indexing, workspace),
        ).tick()
        workspace.delete(owner, document.id)

        class SlowIndexing:
            def commit_sync_document_deleted(self, document_id, mark_processed):
                clock[0] += timedelta(seconds=31)
                indexing.commit_sync_document_deleted(document_id, mark_processed)

        dispatcher = SyncDispatcher(
            service,
            indexing_handlers(SlowIndexing(), workspace),
        )
        result = dispatcher.tick()

        self.assertEqual("PROCESSING", result.status)
        self.assertEqual(("ACTIVE",), tuple(row.status for row in indexing.state.vectors))

    def test_AC_SYNC_005_failure_uses_live_time_and_fixed_diagnostic(self):
        clock = [NOW]

        class AdvanceThenFail:
            def commit(self, event, mark_processed):
                clock[0] += timedelta(seconds=2)
                raise OSError("provider password=secret")

        service = SyncService(clock=lambda: clock[0])
        event = self.deleted_event(service)
        failed = service.dispatch_one("sync-dispatcher", AdvanceThenFail())
        attempt = service.attempts(event.id)[0]

        self.assertEqual(NOW + timedelta(seconds=2), failed.failed_at)
        self.assertEqual(NOW + timedelta(seconds=7), failed.available_at)
        self.assertEqual(HANDLER_FAILURE_TYPE, failed.error_type)
        self.assertEqual(HANDLER_FAILURE_MESSAGE, failed.error_message)
        self.assertNotIn("secret", attempt.error_message)

    def test_AC_SYNC_005_registry_handles_reindex_permission_and_model_events(self):
        indexing = IndexingService(clock=lambda: NOW)
        document = indexing.add_document(status="FAILED")
        version = indexing.add_version(document.id, 1, status="FAILED")
        document.status = "FAILED"
        job = indexing.create_job(version.id, status="FAILED")
        model = indexing.add_model(dimension=3)
        documents = DocumentWorkspace(MemoryStorage(), clock=lambda: NOW)
        service = SyncService(clock=lambda: NOW, dispatcher_enabled=True)
        dispatcher = SyncDispatcher(
            service,
            indexing_handlers(indexing, documents),
        )

        reindex = service.publish(
            idempotency_key=f"DOCUMENT_VERSION:{version.id}:DOCUMENT_REINDEX_REQUESTED:{model.id}:request-1",
            aggregate_type="DOCUMENT_VERSION",
            aggregate_id=version.id,
            aggregate_version=version.version_no,
            event_type="DOCUMENT_REINDEX_REQUESTED",
            payload={"modelId": model.id},
            occurred_at=NOW,
        )
        self.assertEqual("PROCESSED", dispatcher.tick().status)
        self.assertEqual("PENDING", job.status)

        permission = service.publish(
            idempotency_key="PERMISSION:DIRECT_DOCUMENT_PERMISSION:9:PERMISSION_CACHE_REFRESH_REQUESTED:GRANT",
            aggregate_type="PERMISSION",
            aggregate_id=9,
            aggregate_version=None,
            event_type="PERMISSION_CACHE_REFRESH_REQUESTED",
            payload={"action": "GRANT"},
            occurred_at=NOW,
        )
        self.assertEqual("PROCESSED", dispatcher.tick().status)

        activated = service.publish(
            idempotency_key="EMBEDDING_MODEL:1:EMBEDDING_MODEL_ACTIVATED",
            aggregate_type="EMBEDDING_MODEL",
            aggregate_id=model.id,
            aggregate_version=None,
            event_type="EMBEDDING_MODEL_ACTIVATED",
            payload={"modelId": model.id},
            occurred_at=NOW,
        )
        self.assertEqual("PROCESSED", dispatcher.tick().status)
        self.assertEqual(
            ("PROCESSED", "PROCESSED", "PROCESSED"),
            tuple(service.event(row.id).status for row in (reindex, permission, activated)),
        )

    def test_AC_SYNC_006_retry_route_calls_real_service(self):
        service = SyncService(clock=lambda: NOW)
        event = service.publish(
            idempotency_key="DOCUMENT:7:DOCUMENT_DELETED",
            aggregate_type="DOCUMENT",
            aggregate_id=7,
            aggregate_version=None,
            event_type="DOCUMENT_DELETED",
            payload={"documentId": 7},
            occurred_at=NOW,
            max_retries=0,
        )
        service.dispatch_one("sync-dispatcher", _Failure(), NOW)
        app = PlatformApp(principal_resolver=lambda request: ADMIN)
        register_sync_routes(app, service)

        response = app.handle(Request("POST", f"/admin/sync/events/{event.id}/retry"))
        body = json.loads(response.body)
        self.assertEqual((200, "PENDING"), (response.status, body["data"]["status"]))
        self.assertEqual(1, service.event(event.id).max_retries)

    def test_AC_SYNC_008_issue_and_reconcile_routes_call_real_service(self):
        service = SyncService(clock=lambda: NOW)
        repairable = service.add_issue("MISSING_JOB", "ERROR", safe_to_repair=True, now=NOW)
        ignorable = service.add_issue("ORPHANED_DATA", "WARNING", now=NOW)
        app = PlatformApp(principal_resolver=lambda request: ADMIN)
        register_sync_routes(app, service)

        repaired = app.handle(
            Request("POST", f"/admin/sync/issues/{repairable.id}/repair")
        )
        ignored = app.handle(
            Request(
                "POST",
                f"/admin/sync/issues/{ignorable.id}/ignore",
                headers={"Content-Type": "application/json"},
                body=b'{"reason":"false positive"}',
            )
        )
        reconciled = app.handle(
            Request(
                "POST",
                "/admin/sync/reconcile",
                headers={"Content-Type": "application/json"},
                body=b'{"mode":"DRY_RUN"}',
            )
        )
        listed = app.handle(Request("GET", "/admin/sync/issues"))

        self.assertEqual((200, "REPAIRING"), (repaired.status, json.loads(repaired.body)["data"]["status"]))
        self.assertEqual((200, "IGNORED"), (ignored.status, json.loads(ignored.body)["data"]["status"]))
        self.assertEqual((200, "COMPLETED"), (reconciled.status, json.loads(reconciled.body)["data"]["status"]))
        self.assertEqual(2, json.loads(listed.body)["data"]["totalElements"])

    def test_AC_SYNC_009_dispatcher_role_flags_gate_ticks(self):
        service = SyncService(clock=lambda: NOW)
        self.deleted_event(service)
        dispatcher = SyncDispatcher(service, SyncHandlerRegistry())
        self.assertIsNone(dispatcher.tick())
        self.assertEqual((), dispatcher.recovery_tick())
        self.assertIsNone(dispatcher.reconciliation_tick())


if __name__ == "__main__":
    unittest.main()
