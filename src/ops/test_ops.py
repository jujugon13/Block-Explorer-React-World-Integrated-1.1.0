from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src.indexing import IndexingService
from src.infra.http.testing import request_over_uvicorn
from src.ops import (
    DASHBOARD_DESTINATION,
    DashboardDestinationPolicy,
    DashboardPush,
    DashboardService,
    register_ops_routes,
)
from src.platform import PlatformApp
from src.shared import (
    OpsDocumentSnapshot,
    OpsJobSnapshot,
    OpsSearchSnapshot,
    OpsSnapshot,
    OpsWorkerSnapshot,
    Principal,
    Request,
)


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
USER = Principal("user@example.com", frozenset({"USER"}), user_id=1)
ADMIN = Principal("admin@example.com", frozenset({"USER", "ADMIN"}), user_id=2)


class _Reader:
    def __init__(self, snapshot: OpsSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[datetime] = []

    def read_ops_snapshot(self, now: datetime) -> OpsSnapshot:
        self.calls.append(now)
        return self.snapshot


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def publish(self, destination: str, payload) -> None:
        self.calls.append((destination, dict(payload)))


class _Commands:
    def __init__(self) -> None:
        self.calls = []
        self.retried_count = 2

    def manual_retry(self, job_id, now=None):
        self.calls.append(("one", job_id, now))
        return SimpleNamespace(data={"jobId": job_id, "status": "PENDING"})

    def retry_all(self, now=None, on_success=None):
        self.calls.append(("all", now))
        if self.retried_count and on_success is not None:
            on_success()
        return SimpleNamespace(data={
            "scannedCount": self.retried_count,
            "retriedCount": self.retried_count,
            "skippedCount": 0,
            "failedCount": 0,
            "message": f"재처리 {self.retried_count}건, 대상 제외 0건, 오류 0건입니다.",
        })


def _resolver(request: Request):
    return {"Bearer user": USER, "Bearer admin": ADMIN}.get(request.header("authorization"))


def _body(response):
    return json.loads(response.body.decode("utf-8"))


def _request(app, request):
    return request_over_uvicorn(app.handle, request)


class OperationsAcceptanceTests(unittest.TestCase):
    def test_AC_OPS_001_user_cannot_read_summary(self):
        service = DashboardService(_Reader(OpsSnapshot()), clock=lambda: NOW)
        app = PlatformApp(_resolver, lambda: NOW)
        register_ops_routes(app, service)

        response = _request(app,
            Request(
                "GET",
                "/admin/dashboard/summary",
                {"Authorization": "Bearer user"},
            )
        )

        self.assertEqual(403, response.status)
        self.assertEqual("ROLE-002", _body(response)["code"])

    def test_AC_OPS_002_empty_completed_jobs_have_null_average(self):
        snapshot = OpsSnapshot(
            documents=(
                OpsDocumentSnapshot("INDEXED"),
                OpsDocumentSnapshot("UPLOADED"),
                OpsDocumentSnapshot("INDEXING"),
                OpsDocumentSnapshot("FAILED"),
                OpsDocumentSnapshot("DELETED", deleted_at=NOW),
                OpsDocumentSnapshot("DELETED"),
            ),
            jobs=(
                OpsJobSnapshot("PENDING"),
                OpsJobSnapshot("PROCESSING"),
                OpsJobSnapshot("FAILED"),
            ),
            workers=(
                OpsWorkerSnapshot("ACTIVE", NOW - timedelta(seconds=29)),
                OpsWorkerSnapshot("IDLE", NOW - timedelta(seconds=30)),
                OpsWorkerSnapshot("STOPPED", NOW),
            ),
            searches=(
                OpsSearchSnapshot(NOW - timedelta(hours=24)),
                OpsSearchSnapshot(NOW),
                OpsSearchSnapshot(NOW - timedelta(hours=24, microseconds=1)),
                OpsSearchSnapshot(NOW + timedelta(microseconds=1)),
            ),
        )
        reader = _Reader(snapshot)
        service = DashboardService(reader, clock=lambda: NOW)
        app = PlatformApp(_resolver, lambda: NOW)
        register_ops_routes(app, service)
        response = _request(app,
            Request(
                "GET",
                "/admin/dashboard/summary",
                {"Authorization": "Bearer admin"},
            )
        )
        summary = _body(response)["data"]

        self.assertEqual(200, response.status)
        self.assertEqual({"total": 4, "searchable": 1, "pendingIndex": 2}, summary["documents"])
        self.assertEqual(
            {"pending": 1, "processing": 1, "failed": 1, "avgProcessMs": None},
            summary["jobs"],
        )
        self.assertEqual({"activeCount": 1, "totalCount": 3}, summary["workers"])
        self.assertEqual({"recent24hCount": 2}, summary["search"])
        self.assertEqual([NOW], reader.calls)

    def test_AC_OPS_003_user_subscription_is_rejected(self):
        self.assertFalse(DashboardDestinationPolicy.can_subscribe(DASHBOARD_DESTINATION, USER))
        self.assertTrue(DashboardDestinationPolicy.can_subscribe(DASHBOARD_DESTINATION, ADMIN))

    def test_AC_OPS_004_client_send_is_always_rejected(self):
        self.assertFalse(DashboardDestinationPolicy.can_send(DASHBOARD_DESTINATION, USER))
        self.assertFalse(DashboardDestinationPolicy.can_send(DASHBOARD_DESTINATION, ADMIN))

    def test_AC_OPS_005_transitions_are_coalesced_per_tick(self):
        publisher = _Publisher()
        push = DashboardPush(
            DashboardService(_Reader(OpsSnapshot()), clock=lambda: NOW),
            publisher,
        )

        for _ in range(10):
            push.state_transition_committed()

        self.assertEqual(0.3, push.debounce_seconds)
        self.assertTrue(push.tick())
        self.assertFalse(push.tick())
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(DASHBOARD_DESTINATION, publisher.calls[0][0])

    def test_AC_OPS_005_retry_routes_mark_push_only_after_success(self):
        commands, publisher = _Commands(), _Publisher()
        service = DashboardService(
            _Reader(OpsSnapshot()), clock=lambda: NOW, indexing_commands=commands
        )
        push = DashboardPush(service, publisher)
        app = PlatformApp(_resolver, lambda: NOW)
        register_ops_routes(app, service, push)

        one = _request(app, Request(
            "POST",
            "/admin/embedding-jobs/7/retry",
            {"Authorization": "Bearer admin"},
        ))
        self.assertEqual((200, 7), (one.status, _body(one)["data"]["jobId"]))
        self.assertTrue(push.tick())

        all_response = _request(app, Request(
            "POST",
            "/admin/embedding-jobs/retry-all",
            {"Authorization": "Bearer admin"},
        ))
        self.assertEqual((200, 2),
                         (all_response.status, _body(all_response)["data"]["retriedCount"]))
        self.assertTrue(push.tick())
        self.assertEqual(2, len(publisher.calls))

        commands.retried_count = 0
        _request(app, Request(
            "POST",
            "/admin/embedding-jobs/retry-all",
            {"Authorization": "Bearer admin"},
        ))
        self.assertFalse(push.tick())

    def test_AC_OPS_005_actual_indexing_retry_response_is_json_safe(self):
        indexing = IndexingService(clock=lambda: NOW)
        document = indexing.add_document(status="FAILED")
        version = indexing.add_version(document.id, 1, status="FAILED")
        job = indexing.create_job(version.id, status="FAILED")
        job.first_started_at = NOW - timedelta(minutes=1)
        job.failed_at = NOW
        job.error_message = "private backend detail"
        service = DashboardService(
            _Reader(OpsSnapshot()),
            clock=lambda: NOW,
            indexing_commands=indexing,
        )
        app = PlatformApp(_resolver, lambda: NOW)
        register_ops_routes(app, service)

        response = _request(app, Request(
            "POST",
            f"/admin/embedding-jobs/{job.id}/retry",
            {"Authorization": "Bearer admin"},
        ))
        body = _body(response)
        self.assertEqual(200, response.status)
        self.assertEqual(
            {"jobId": job.id, "status": "PENDING"}, body["data"]
        )
        self.assertNotIn("private backend detail", json.dumps(body))

    def test_AC_IDX_065_AC_IDX_066_retry_all_over_uvicorn(self):
        indexing = IndexingService(clock=lambda: NOW)
        for number in range(3):
            document = indexing.add_document(status="FAILED")
            version = indexing.add_version(document.id, 1, status="FAILED")
            indexing.create_job(version.id, status="FAILED")
            if number == 2:
                indexing.add_version(document.id, 2, status="UPLOADED")
        service = DashboardService(
            _Reader(OpsSnapshot()), clock=lambda: NOW, indexing_commands=indexing
        )
        app = PlatformApp(_resolver, lambda: NOW)
        register_ops_routes(app, service)
        partial = _request(
            app,
            Request(
                "POST",
                "/admin/embedding-jobs/retry-all",
                {"Authorization": "Bearer admin"},
            ),
        )
        data = _body(partial)["data"]
        self.assertEqual((200, 2, 1, 0), (
            partial.status,
            data["retriedCount"],
            data["skippedCount"],
            data["failedCount"],
        ))

        empty = IndexingService(clock=lambda: NOW)
        service = DashboardService(
            _Reader(OpsSnapshot()), clock=lambda: NOW, indexing_commands=empty
        )
        app = PlatformApp(_resolver, lambda: NOW)
        register_ops_routes(app, service)
        response = _request(
            app,
            Request(
                "POST",
                "/admin/embedding-jobs/retry-all",
                {"Authorization": "Bearer admin"},
            ),
        )
        data = _body(response)["data"]
        self.assertEqual(
            (200, 0, 0, 0, 0),
            (
                response.status,
                data["scannedCount"],
                data["retriedCount"],
                data["skippedCount"],
                data["failedCount"],
            ),
        )

    def test_AC_OPS_006_rolled_back_transition_is_not_pushed(self):
        publisher = _Publisher()
        push = DashboardPush(
            DashboardService(_Reader(OpsSnapshot()), clock=lambda: NOW),
            publisher,
        )

        push.state_transition_rolled_back()

        self.assertFalse(push.tick())
        self.assertEqual([], publisher.calls)

    def test_AC_OPS_005_failed_push_restores_the_change_flag(self):
        class FailsOnce(_Publisher):
            def publish(self, destination: str, payload) -> None:
                super().publish(destination, payload)
                if len(self.calls) == 1:
                    raise RuntimeError("broker unavailable")

        publisher = FailsOnce()
        push = DashboardPush(
            DashboardService(_Reader(OpsSnapshot()), clock=lambda: NOW),
            publisher,
        )
        push.state_transition_committed()

        with self.assertRaises(RuntimeError):
            push.tick()
        self.assertTrue(push.tick())
        self.assertEqual(2, len(publisher.calls))

    def test_AC_OPS_002_completed_jobs_use_full_process_duration(self):
        snapshot = OpsSnapshot(
            jobs=(
                OpsJobSnapshot("INDEXED", NOW - timedelta(seconds=3), NOW),
                OpsJobSnapshot("INDEXED", NOW - timedelta(seconds=1), NOW),
                OpsJobSnapshot("FAILED", NOW - timedelta(seconds=9), NOW),
            )
        )
        summary = DashboardService(_Reader(snapshot), clock=lambda: NOW).summary()
        self.assertEqual(2000.0, summary["jobs"]["avgProcessMs"])


if __name__ == "__main__":
    unittest.main()
