from __future__ import annotations

import json
import threading
import unittest
from datetime import UTC, datetime, timedelta

from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.infra.http.testing import request_over_uvicorn
from src.shared import PublicError
from src.platform import PlatformApp
from src.shared import Principal, Request
from src.sync import SyncService, register_sync_routes


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)


class SuccessUnitOfWork:
    def commit(self, event, mark_processed):
        mark_processed()


class FailureUnitOfWork:
    def commit(self, event, mark_processed):
        raise OSError("temporary failure")


class DeletedDocumentUnitOfWork:
    def __init__(self):
        self.document_status = "DELETED"
        self.deleted_at = NOW
        self.vector_statuses = ["ACTIVE", "ACTIVE", "STALE"]

    def commit(self, event, mark_processed):
        if self.document_status != "DELETED" or self.deleted_at is None:
            raise RuntimeError("document is not deleted")
        staged = ["STALE" if status == "ACTIVE" else status for status in self.vector_statuses]
        mark_processed()
        self.vector_statuses = staged


class SyncAcceptanceTests(unittest.TestCase):
    def service(self, **overrides) -> SyncService:
        return SyncService(clock=lambda: NOW, **overrides)

    def assert_code(self, code: str, call) -> None:
        with self.assertRaises(PublicError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    @staticmethod
    def publish(service: SyncService, *, max_retries: int | None = None):
        return service.publish(
            idempotency_key="DOCUMENT:7:DOCUMENT_DELETED",
            aggregate_type="DOCUMENT",
            aggregate_id=7,
            aggregate_version=None,
            event_type="DOCUMENT_DELETED",
            payload={"documentId": 7},
            occurred_at=NOW,
            max_retries=max_retries,
        )

    def test_AC_SYNC_001_document_upload_publishes_one_version_event(self):
        service = self.service()
        workspace = DocumentWorkspace(
            MemoryStorage(), sync_outbox=service, clock=lambda: NOW
        )
        workspace.upload(
            Principal("owner", user_id=7),
            UploadFile(b"text", "note.txt", "text/plain"),
            title="Title",
            description=None,
            visibility="PRIVATE",
        )
        events = service.events(event_type="DOCUMENT_VERSION_CREATED")
        self.assertEqual(1, len(events))
        self.assertEqual("PENDING", events[0].status)
        self.assertEqual({"documentId": 1, "versionId": 1}, events[0].payload)

    def test_AC_SYNC_002_concurrent_same_key_converges_to_one_event(self):
        service = self.service()
        barrier = threading.Barrier(2)
        returned: list[str] = []

        def run(payload):
            barrier.wait()
            event = service.publish(
                idempotency_key="same-key",
                aggregate_type="DOCUMENT_VERSION",
                aggregate_id=11,
                aggregate_version=1,
                event_type="DOCUMENT_VERSION_CREATED",
                payload=payload,
                occurred_at=NOW,
            )
            returned.append(event.id)

        threads = [
            threading.Thread(target=run, args=({"a": 1, "b": 2},)),
            threading.Thread(target=run, args=({"b": 2, "a": 1},)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(service.events()))
        self.assertEqual(2, len(returned))
        self.assertEqual(1, len(set(returned)))

        numeric = service.publish(
            idempotency_key="numeric-key",
            aggregate_type="DOCUMENT_VERSION",
            aggregate_id=12,
            aggregate_version=1,
            event_type="DOCUMENT_VERSION_CREATED",
            payload={"value": 1},
            occurred_at=NOW,
        )
        replay = service.publish(
            idempotency_key="numeric-key",
            aggregate_type="DOCUMENT_VERSION",
            aggregate_id=12,
            aggregate_version=1,
            event_type="DOCUMENT_VERSION_CREATED",
            payload={"value": 1.0},
            occurred_at=NOW,
        )
        self.assertEqual(numeric.id, replay.id)
        self.assert_code(
            "SYNC-003",
            lambda: service.publish(
                idempotency_key="numeric-key",
                aggregate_type="DOCUMENT_VERSION",
                aggregate_id=12,
                aggregate_version=1,
                event_type="DOCUMENT_VERSION_CREATED",
                payload={"value": True},
                occurred_at=NOW,
            ),
        )

    def test_AC_SYNC_003_same_key_different_payload_is_sync_003(self):
        service = self.service()
        self.publish(service)
        self.assert_code(
            "SYNC-003",
            lambda: service.publish(
                idempotency_key="DOCUMENT:7:DOCUMENT_DELETED",
                aggregate_type="DOCUMENT",
                aggregate_id=7,
                aggregate_version=None,
                event_type="DOCUMENT_DELETED",
                payload={"documentId": 8},
                occurred_at=NOW,
            ),
        )

    def test_AC_SYNC_004_document_delete_stales_active_vectors(self):
        service = self.service()
        event = self.publish(service)
        unit = DeletedDocumentUnitOfWork()
        processed = service.dispatch_one("sync-dispatcher", unit, NOW)
        self.assertEqual(["STALE", "STALE", "STALE"], unit.vector_statuses)
        self.assertEqual("PROCESSED", processed.status)
        self.assertEqual("SUCCEEDED", service.attempts(event.id)[0].status)

    def test_AC_SYNC_005_handler_failure_requeues_then_finally_fails(self):
        service = self.service()
        event = self.publish(service)
        moment = NOW
        for failure in range(1, 7):
            event = service.dispatch_one("sync-dispatcher", FailureUnitOfWork(), moment)
            self.assertEqual(failure, event.failure_count)
            if failure <= 5:
                self.assertEqual("PENDING", event.status)
                expected = (5, 10, 20, 40, 60)[failure - 1]
                self.assertEqual(moment + timedelta(seconds=expected), event.available_at)
                moment = event.available_at
        self.assertEqual("FAILED", event.status)
        self.assertEqual(6, len(service.attempts(event.id)))

    def test_AC_SYNC_006_processed_event_manual_retry_is_sync_004(self):
        service = self.service()
        event = self.publish(service)
        service.dispatch_one("sync-dispatcher", SuccessUnitOfWork(), NOW)
        self.assert_code(
            "SYNC-004", lambda: service.retry_failed(event.id, actor_id=1, now=NOW)
        )

        app = PlatformApp(principal_resolver=lambda request: Principal("admin", frozenset({"ADMIN"})))

        def fail(request):
            raise PublicError("SYNC-004")

        app.add_route("POST", "/admin/sync/events/{eventId}/retry", fail)
        response = request_over_uvicorn(
            app.handle, Request("POST", f"/admin/sync/events/{event.id}/retry")
        )
        body = json.loads(response.body)
        self.assertEqual((409, "SYNC-004", "최종 실패한 동기화 Event만 재처리할 수 있습니다."),
                         (response.status, body["code"], body["message"]))

    def test_AC_SYNC_007_failed_manual_retry_adds_exactly_one_chance(self):
        service = self.service()
        event = self.publish(service, max_retries=0)
        failed = service.dispatch_one("sync-dispatcher", FailureUnitOfWork(), NOW)
        self.assertEqual("FAILED", failed.status)
        retried = service.retry_failed(event.id, actor_id=1, now=NOW)
        self.assertEqual("PENDING", retried.status)
        self.assertEqual(1, retried.max_retries)
        self.assertEqual(1, retried.failure_count)
        self.assertIsNone(retried.error_message)

    def test_AC_SYNC_008_resolved_issue_cannot_be_ignored(self):
        service = self.service()
        issue = service.add_issue("MISSING_JOB", "ERROR", now=NOW)
        issue.status = "RESOLVED"
        app = PlatformApp(
            principal_resolver=lambda _request: Principal(
                "admin", frozenset({"ADMIN"}), user_id=1
            )
        )
        register_sync_routes(app, service)
        response = request_over_uvicorn(
            app.handle,
            Request(
                "POST",
                f"/admin/sync/issues/{issue.id}/ignore",
                {"Content-Type": "application/json"},
                body=json.dumps({"reason": "false positive"}).encode(),
            ),
        )
        body = json.loads(response.body)
        self.assertEqual((409, "SYNC-007"), (response.status, body["code"]))

    def test_AC_SYNC_009_expired_lease_requeues_or_finally_fails(self):
        service = self.service()
        pending = self.publish(service, max_retries=1)
        service.claim("sync-dispatcher", NOW)
        second = service.publish(
            idempotency_key="DOCUMENT:8:DOCUMENT_DELETED",
            aggregate_type="DOCUMENT",
            aggregate_id=8,
            aggregate_version=None,
            event_type="DOCUMENT_DELETED",
            payload={"documentId": 8},
            occurred_at=NOW,
            max_retries=1,
        )
        second.failure_count = 1
        service.claim("sync-dispatcher", NOW)
        recovered = service.recover_expired(NOW + timedelta(seconds=30))
        self.assertEqual(2, len(recovered))
        self.assertEqual("PENDING", service.event(pending.id).status)
        self.assertEqual("FAILED", service.event(second.id).status)
        self.assertEqual(1, service.event(pending.id).failure_count)
        self.assertEqual(2, service.event(second.id).failure_count)


if __name__ == "__main__":
    unittest.main()
