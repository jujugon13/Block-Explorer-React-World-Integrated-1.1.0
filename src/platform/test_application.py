from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from src.application import ApplicationComponents, create_application
from src.auth import AuthService, InMemoryCache, TokenManager
from src.collections import CollectionWorkspace
from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.guardrails import GuardrailService
from src.indexing import IndexingService
from src.mcp import InMemoryMcpTokenStore, McpApplicationBackend, McpService
from src.ops import CompositeOpsSnapshotReader, DashboardService
from src.permissions import PermissionService
from src.platform import (
    InMemoryStompBroker,
    StompConnectionRejected,
    StompFrame,
)
from src.search import InMemorySearchHistory, SearchPorts, SearchService
from src.shared import Request
from src.sync import SyncService
from src.users import UserDirectory


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
SETTINGS = {
    "jwt.secret": "application-secret",
    "storage.type": "local",
    "sync.dispatcher.enabled": True,
}
SEARCH_SETTINGS = {
    "search_mode": "vector",
    "multi_query_enabled": False,
    "hyde_enabled": False,
    "document_scope_enabled": False,
    "reranking_enabled": False,
    "retrieval_quality_gate_enabled": False,
    "pii_detection_enabled": False,
    "injection_detection_enabled": False,
    "numeric_verification_enabled": False,
    "faithfulness_enabled": False,
    "hallucination_detection_enabled": False,
    "generate_answer": False,
    "cache_enabled": False,
}


class _NoResults:
    def search(self, *args, **kwargs):
        return ()


class _Embedder:
    def embed_query(self, text: str):
        return (1.0,)


class _LanguageModel:
    def complete(self, request):
        return ""


def _components() -> tuple[ApplicationComponents, str]:
    users = UserDirectory()
    users.seed_department(1, "Engineering")
    users.seed_role("USER")
    users.seed_role("ADMIN")
    user = users.create_user(
        "admin@example.com", "unused", "Admin", 1, NOW
    )
    users.assign_role(user.id, "ADMIN", granted_by_user_id=user.id, now=NOW)

    tokens = TokenManager(str(SETTINGS["jwt.secret"]))
    auth = AuthService(
        users,
        InMemoryCache(lambda: NOW),
        tokens,
        clock=lambda: NOW,
    )
    bearer = "Bearer " + tokens.issue(user.id, user.email, NOW)
    principal = auth.resolve_request(Request("GET", "/auth/me", {
        "Authorization": bearer
    }))
    if principal is None:
        raise AssertionError("test principal must resolve")

    indexing = IndexingService(clock=lambda: NOW)
    documents = DocumentWorkspace(
        MemoryStorage(), indexing=indexing, clock=lambda: NOW
    )
    collections = CollectionWorkspace(documents=documents)
    permissions = PermissionService(
        documents, collections, clock=lambda: NOW
    )
    documents.upload(
        principal,
        UploadFile(b"hello", "smoke.txt", "text/plain"),
        title="Smoke",
        description=None,
        visibility="PRIVATE",
    )

    history = InMemorySearchHistory()
    search = SearchService(
        SearchPorts(
            indexing,
            permissions,
            _Embedder(),
            _NoResults(),
            _NoResults(),
            _LanguageModel(),
            GuardrailService(sleep=lambda _: None),
            history,
        ),
        SEARCH_SETTINGS,
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )
    sync = SyncService(clock=lambda: NOW)
    mcp = McpService(
        McpApplicationBackend(search, documents, permissions),
        token_store=InMemoryMcpTokenStore(),
        clock=lambda: NOW,
        principal_factory=lambda user_id: principal if user_id == user.id else None,
    )
    ops = DashboardService(
        CompositeOpsSnapshotReader(documents, indexing, history),
        clock=lambda: NOW,
        indexing_commands=indexing,
    )
    return ApplicationComponents(
        auth=auth,
        users=users,
        documents=documents,
        collections=collections,
        permissions=permissions,
        indexing=indexing,
        search=search,
        sync=sync,
        mcp=mcp,
        ops=ops,
        chunk_producer=lambda job_id, attempt_id, worker_id, token: (),
        embedder=lambda chunks, model: (),
    ), bearer


class _WorkerLifecycle:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def shutdown(self) -> None:
        self.stops += 1


class _Loop:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = Event()
        self.exited = Event()

    def run(self, stop: Event) -> None:
        self.calls += 1
        self.entered.set()
        stop.wait()
        self.exited.set()

    def state_transition_committed(self) -> None:
        pass


class _RetentionLoop(_Loop):
    def __init__(self) -> None:
        super().__init__()
        self.interval: float | None = None

    def serve(self, stop: Event, *, interval_seconds: float) -> None:
        self.interval = interval_seconds
        self.run(stop)


class _FlakyLoop:
    def __init__(self, *, delay: float = 0.01) -> None:
        self.calls = 0
        self.entered = Event()
        self.exited = Event()
        self.debounce_seconds = delay
        self.service = SimpleNamespace(poll_interval=timedelta(seconds=delay))

    def _call(self, stop: Event) -> None:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("password=background-secret")
        self.entered.set()
        stop.wait()
        self.exited.set()

    def run(self, stop: Event) -> None:
        self._call(stop)

    def serve(self, stop: Event, *, interval_seconds: float) -> None:
        del interval_seconds
        self._call(stop)

    def state_transition_committed(self) -> None:
        pass


class _ReturningLoop:
    def __init__(self) -> None:
        self.calls = 0
        self.service = SimpleNamespace(poll_interval=timedelta(seconds=0.01))

    def run(self, stop: Event) -> None:
        del stop
        self.calls += 1

    def state_transition_committed(self) -> None:
        pass


class ApplicationTests(unittest.TestCase):
    def test_AC_SYS_009_missing_or_blank_jwt_secret_fails_startup(self):
        for secret in (None, "", "   "):
            with self.subTest(secret=secret), self.assertRaisesRegex(
                ValueError, "jwt.secret"
            ):
                create_application({"jwt.secret": secret}, object())

        invalid_settings = (
            {"indexing.worker.heartbeat-interval": 30},
            {"indexing.worker.poll-interval": 11},
            {"indexing.worker.lease-duration": 0},
            {"indexing.worker.lease-renew-interval": 300},
            {"indexing.worker.recovery-interval": 0},
            {"indexing.worker.retry-initial": 0},
            {"indexing.worker.retry-initial": 10, "indexing.worker.retry-max": 9},
            {"indexing.worker.shutdown-grace": -1},
            {"embedding.circuit-open-seconds": 0},
            {"embedding.connect-timeout": 0},
            {"embedding.response-timeout": 0},
            {"embedding.document-batch-response-timeout": 0},
            {"embedding.circuit-enabled": 1},
            {"indexing.job.max-retries": -1},
            {"dashboard.push-debounce": 0},
            {"scheduler.background-worker-pool": 0},
            {"sync.dispatcher.poll-interval": 0},
            {"sync.dispatcher.retry-initial": 5, "sync.dispatcher.retry-max": 4},
            {"sync.dispatcher.enabled": "true"},
            {"sync.reconciliation.enabled": 1},
            {"sync.reconciliation.period": 0},
            {"document.parsing.handlers": ()},
            {"document.parsing.handlers": ("PDF", "pdf")},
            {"document.parsing.handlers": ((),)},
            {"indexing.worker.poll-interval": float("nan")},
            {"document.chunking.chunk-size": 1000.5},
        )
        for values in invalid_settings:
            with self.subTest(values=values), self.assertRaises(ValueError):
                create_application({**SETTINGS, **values}, object())

    def test_AC_SYS_010_external_storage_requires_bucket_namespace(self):
        for storage_type in ("minio", "s3"):
            with self.subTest(storage_type=storage_type), self.assertRaisesRegex(
                ValueError, "bucket/namespace"
            ):
                create_application(
                    {"jwt.secret": "secret", "storage.type": storage_type},
                    object(),
                )

    def test_FR_SYS_003_disabled_sync_roles_omit_supplied_dispatcher_and_apply_debounce(self):
        components, _ = _components()
        application = create_application(
            {
                **SETTINGS,
                "sync.dispatcher.enabled": False,
                "sync.reconciliation.enabled": False,
                "dashboard.push-debounce": 0.01,
            },
            replace(components, sync_dispatcher=_Loop()),
        )

        self.assertIsNone(application.components.sync_dispatcher)
        self.assertEqual(0.01, application.components.dashboard_push.debounce_seconds)

    def test_AC_DOC_064_chunk_bounds_fail_at_the_real_startup_boundary(self):
        for size, overlap in ((1000, -1), (1000, 1000), (0, 0)):
            with self.subTest(size=size, overlap=overlap), self.assertRaises(ValueError):
                create_application(
                    {
                        **SETTINGS,
                        "document.chunking.chunk-size": size,
                        "document.chunking.overlap": overlap,
                    },
                    object(),
                )

    def test_AC_SYS_003_all_feature_routes_handle_real_smoke_requests(self):
        components, bearer = _components()
        application = create_application(SETTINGS, components)
        authorized = {"Authorization": bearer}
        requests = (
            Request("GET", "/auth/me", authorized),
            Request("GET", "/departments"),
            Request("GET", "/api/documents", authorized),
            Request("GET", "/collections", authorized),
            Request("GET", "/permissions/documents/1/me", authorized),
            Request("GET", "/admin/indexing-jobs", authorized),
            Request(
                "POST",
                "/api/search",
                {**authorized, "Content-Type": "application/json"},
                json.dumps({"query": "smoke"}).encode(),
            ),
            Request("GET", "/admin/sync/summary", authorized),
            Request("POST", "/mcp/tokens", authorized),
            Request("GET", "/admin/dashboard/summary", authorized),
        )

        self.assertEqual(
            (200, 200, 200, 200, 200, 200, 200, 200, 201, 200),
            tuple(application.handle(request).status for request in requests),
        )

    def test_AC_SYS_004_runtime_keeps_authenticated_stomp_processor_and_broker(self):
        components, bearer = _components()
        broker = InMemoryStompBroker()
        application = create_application(
            SETTINGS, replace(components, stomp_broker=broker)
        )
        frames: list[StompFrame] = []
        rejected = application.stomp_processor.handshake(None, frames.append)
        with self.assertRaises(StompConnectionRejected):
            application.stomp_processor.process(rejected, StompFrame("CONNECT"))

        session = application.stomp_processor.handshake(None, frames.append)
        connected = application.stomp_processor.process(
            session, StompFrame("CONNECT", {"Authorization": bearer})
        )
        application.stomp_processor.process(
            session,
            StompFrame(
                "SUBSCRIBE",
                {"id": "dashboard", "destination": "/topic/dashboard"},
            ),
        )

        self.assertEqual("CONNECTED", connected.command if connected else None)
        self.assertIs(broker, application.stomp_broker)
        self.assertEqual(1, broker.subscriber_count("/topic/dashboard"))

    def test_AC_SYS_004_sockjs_xhr_path_authenticates_connect_and_delivers_message(self):
        components, bearer = _components()
        application = create_application(SETTINGS, components)
        info = application.handle(Request("GET", "/ws/info"))
        opened = application.handle(Request("POST", "/ws/000/browser/xhr"))
        self.assertEqual((200, True), (info.status, json.loads(info.body)["websocket"]))
        self.assertEqual((200, b"o\n"), (opened.status, opened.body))

        connect = "CONNECT\nAuthorization:" + bearer + "\n\n\x00"
        sent = application.handle(
            Request(
                "POST",
                "/ws/000/browser/xhr_send",
                {"Content-Type": "application/json"},
                json.dumps([connect]).encode(),
            )
        )
        connected = application.handle(Request("POST", "/ws/000/browser/xhr"))
        connected_frames = json.loads(connected.body[1:])
        self.assertEqual(204, sent.status)
        self.assertTrue(connected_frames[0].startswith("CONNECTED\n"))

        subscribe = (
            "SUBSCRIBE\nid:dashboard\ndestination:/topic/dashboard\n\n\x00"
        )
        application.handle(
            Request(
                "POST",
                "/ws/000/browser/xhr_send",
                {"Content-Type": "application/json"},
                json.dumps([subscribe]).encode(),
            )
        )
        application.stomp_broker.publish(
            "/topic/dashboard", b'{"ok":true}', "application/json"
        )
        delivered = application.handle(Request("POST", "/ws/000/browser/xhr"))
        delivered_frames = json.loads(delivered.body[1:])
        self.assertTrue(delivered_frames[0].startswith("MESSAGE\n"))
        self.assertIn('{"ok":true}', delivered_frames[0])

    def test_AC_OPS_005_start_stop_runs_each_optional_runtime_once(self):
        components, _ = _components()
        worker = _WorkerLifecycle()
        sync, retention, dashboard = _Loop(), _RetentionLoop(), _Loop()
        application = create_application(
            SETTINGS,
            replace(
                components,
                worker_runtime=worker,
                sync_dispatcher=sync,
                search_history_retention_job=retention,
                dashboard_push=dashboard,
            ),
        )

        application.start()
        application.start()
        self.assertTrue(sync.entered.wait(1.0))
        self.assertTrue(retention.entered.wait(1.0))
        self.assertTrue(dashboard.entered.wait(1.0))
        self.assertEqual((1, 1, 1, 1), (
            worker.starts, sync.calls, retention.calls, dashboard.calls
        ))
        self.assertTrue(all(thread.daemon for thread in application.background_threads))

        application.stop()
        application.stop()
        self.assertEqual(1, worker.stops)
        self.assertTrue(sync.exited.wait(1.0))
        self.assertTrue(retention.exited.wait(1.0))
        self.assertTrue(dashboard.exited.wait(1.0))
        self.assertFalse(application.started)

    def test_IT_STAGE16_background_supervisor_restarts_only_failed_targets(self):
        components, _ = _components()
        sync, retention, dashboard = _FlakyLoop(), _FlakyLoop(), _FlakyLoop()
        application = create_application(
            SETTINGS,
            replace(
                components,
                sync_dispatcher=sync,
                search_history_retention_job=retention,
                search_history_retention_interval_seconds=0.01,
                dashboard_push=dashboard,
            ),
        )

        with self.assertLogs("src.application", level="ERROR") as logs:
            application.start()
            self.assertTrue(sync.entered.wait(1.0))
            self.assertTrue(retention.entered.wait(1.0))
            self.assertTrue(dashboard.entered.wait(1.0))
            self.assertTrue(all(thread.is_alive() for thread in application.background_threads))
            application.stop()

        self.assertEqual((2, 2, 2), (sync.calls, retention.calls, dashboard.calls))
        self.assertTrue(sync.exited.is_set() and retention.exited.is_set() and dashboard.exited.is_set())
        output = "\n".join(logs.output)
        self.assertEqual(3, output.count("ConnectionError"))
        self.assertIn("vectorshelf-sync", output)
        self.assertIn("vectorshelf-search-history-retention", output)
        self.assertIn("vectorshelf-dashboard-push", output)
        self.assertNotIn("background-secret", output)

    def test_IT_STAGE16_background_supervisor_does_not_restart_normal_return(self):
        components, _ = _components()
        returned = _ReturningLoop()
        application = create_application(
            SETTINGS,
            replace(
                components,
                sync_dispatcher=returned,
                search_history_retention_job=None,
                dashboard_push=None,
            ),
        )

        with patch("src.application.DashboardPush.run", return_value=None):
            application.start()
            application.background_threads[0].join(1.0)
            application.stop()

        self.assertEqual(1, returned.calls)

    def test_IT_STAGE16_enabled_worker_uses_configured_runtime_and_real_indexing_path(self):
        components, _ = _components()
        components.indexing._clock = lambda: datetime.now(UTC)
        components.indexing.add_model(dimension=3)
        order: list[str] = []

        def chunks(*args):
            del args
            order.append("chunk_producer")
            return ("hello",)

        def embeddings(records, model):
            del model
            order.append("embedder")
            return ((1.0, 2.0, 3.0),) * len(records)

        application = create_application(
            {
                **SETTINGS,
                "indexing.worker.enabled": True,
                "indexing.worker.name": "stage16-worker",
                "indexing.worker.heartbeat-interval": 2,
                "indexing.worker.dead-threshold": 9,
                "indexing.worker.poll-interval": 1,
                "indexing.worker.empty-poll-max": 4,
                "indexing.worker.max-concurrency": 1,
                "indexing.worker.lease-duration": 8,
                "indexing.worker.lease-renew-interval": 3,
                "indexing.worker.recovery-interval": 5,
                "indexing.worker.recovery-batch-size": 7,
                "indexing.worker.shutdown-grace": 6,
            },
            replace(components, chunk_producer=chunks, embedder=embeddings),
        )
        runtime = application.components.worker_runtime
        self.assertIsNotNone(runtime)
        self.assertIs(components.indexing, runtime.backend)
        self.assertEqual(
            (True, "stage16-worker", 2, 9, 1, 4, 1, 8, 3, 5, 7, 6),
            (
                runtime.config.enabled,
                runtime.config.name,
                runtime.config.heartbeat_interval,
                runtime.config.dead_threshold,
                runtime.config.poll_interval,
                runtime.config.empty_poll_max,
                runtime.config.max_concurrency,
                runtime.config.lease_duration,
                runtime.config.lease_renew_interval,
                runtime.config.recovery_interval,
                runtime.config.recovery_batch_size,
                runtime.config.shutdown_grace,
            ),
        )

        originals = {
            name: getattr(components.indexing, name)
            for name in ("start_attempt", "save_chunks", "save_embeddings", "complete")
        }

        def recorded(name):
            def call(*args, **kwargs):
                order.append(name)
                return originals[name](*args, **kwargs)

            return call

        with (
            patch.object(components.indexing, "start_attempt", side_effect=recorded("start_attempt")),
            patch.object(components.indexing, "save_chunks", side_effect=recorded("save_chunks")),
            patch.object(components.indexing, "save_embeddings", side_effect=recorded("save_embeddings")),
            patch.object(components.indexing, "complete", side_effect=recorded("complete")),
        ):
            runtime.start(background=False)
            self.assertEqual(1, runtime.poll_tick())
            self.assertTrue(runtime.wait_for_idle(1.0))
            runtime.shutdown()

        self.assertEqual(
            (
                "start_attempt",
                "save_chunks",
                "chunk_producer",
                "save_embeddings",
                "embedder",
                "complete",
            ),
            tuple(order),
        )
        self.assertEqual("INDEXED", next(iter(components.indexing.state.jobs.values())).status)

    def test_IT_STAGE16_worker_failure_uses_existing_failure_report_path(self):
        components, _ = _components()
        components.indexing._clock = lambda: datetime.now(UTC)
        components.indexing.add_model(dimension=3)

        def fail_chunks(*args):
            del args
            raise ValueError("parser failed")

        application = create_application(
            {**SETTINGS, "indexing.worker.enabled": True},
            replace(components, chunk_producer=fail_chunks),
        )
        runtime = application.components.worker_runtime
        self.assertIsNotNone(runtime)
        original = components.indexing.report_failure_from_worker
        with patch.object(
            components.indexing,
            "report_failure_from_worker",
            wraps=original,
        ) as report:
            runtime.start(background=False)
            self.assertEqual(1, runtime.poll_tick())
            self.assertTrue(runtime.wait_for_idle(1.0))
            runtime.shutdown()

        report.assert_called_once()
        job = next(iter(components.indexing.state.jobs.values()))
        self.assertEqual(("PENDING", "WORKER_INTERNAL_ERROR"), (job.status, job.failure_type))

    def test_IT_STAGE16_disabled_worker_is_not_composed_or_registered(self):
        components, _ = _components()
        application = create_application(
            {**SETTINGS, "indexing.worker.enabled": False}, components
        )

        self.assertIsNone(application.components.worker_runtime)
        application.start()
        application.stop()
        self.assertEqual((), tuple(components.indexing.state.workers.values()))

    def test_AC_SYS_003_composition_wires_permissions_user_search_and_sync_outbox(self):
        components, bearer = _components()
        reader = components.users.create_user(
            "reader@example.com", "unused", "Reader", 1, NOW
        )
        reader_bearer = "Bearer " + components.auth.tokens.issue(
            reader.id, reader.email, NOW
        )
        application = create_application(SETTINGS, components)
        json_headers = {
            "Authorization": bearer,
            "Content-Type": "application/json",
        }

        grant_document = application.handle(
            Request(
                "POST",
                "/permissions/documents/1",
                json_headers,
                json.dumps(
                    {
                        "permissionKind": "READ",
                        "targetType": "USER",
                        "userId": reader.id,
                    }
                ).encode(),
            )
        )
        reader_document = application.handle(
            Request("GET", "/api/documents/1", {"Authorization": reader_bearer})
        )
        users = application.handle(
            Request(
                "GET",
                "/permissions/documents/1/users?keyword=reader",
                {"Authorization": bearer},
            )
        )

        collection = application.handle(
            Request(
                "POST",
                "/collections",
                json_headers,
                b'{"name":"Root"}',
            )
        )
        collection_id = json.loads(collection.body)["data"]["collectionId"]
        grant_collection = application.handle(
            Request(
                "POST",
                f"/permissions/collections/{collection_id}",
                json_headers,
                json.dumps(
                    {
                        "permissionKind": "WRITE",
                        "targetType": "USER",
                        "userId": reader.id,
                    }
                ).encode(),
            )
        )
        child = application.handle(
            Request(
                "POST",
                "/collections",
                {
                    "Authorization": reader_bearer,
                    "Content-Type": "application/json",
                },
                json.dumps({"name": "Child", "parentId": collection_id}).encode(),
            )
        )

        user_rows = json.loads(users.body)["data"]
        self.assertEqual((201, 200, 200, 201, 201), (
            grant_document.status,
            reader_document.status,
            users.status,
            grant_collection.status,
            child.status,
        ))
        self.assertEqual(
            {"userId", "email", "name", "departmentId", "departmentName"},
            set(user_rows[0]),
        )
        self.assertEqual("reader@example.com", user_rows[0]["email"])
        self.assertEqual(
            2,
            sum(
                event.event_type == "PERMISSION_CACHE_REFRESH_REQUESTED"
                for event in components.sync.state.events.values()
            ),
        )

    def test_AC_OPS_005_default_runtime_pushes_only_committed_changes(self):
        components, bearer = _components()
        application = create_application(SETTINGS, components)
        frames: list[StompFrame] = []
        session = application.stomp_processor.handshake(None, frames.append)
        application.stomp_processor.process(
            session, StompFrame("CONNECT", {"Authorization": bearer})
        )
        application.stomp_processor.process(
            session,
            StompFrame(
                "SUBSCRIBE",
                {"id": "dashboard", "destination": "/topic/dashboard"},
            ),
        )

        changed = application.handle(
            Request(
                "POST",
                "/collections",
                {
                    "Authorization": bearer,
                    "Content-Type": "application/json",
                },
                b'{"name":"Push"}',
            )
        )
        self.assertEqual(201, changed.status)
        self.assertTrue(application.components.dashboard_push.tick())
        self.assertEqual("MESSAGE", frames[-1].command)
        delivered = len(frames)

        rolled_back = application.handle(
            Request(
                "POST",
                "/collections",
                {
                    "Authorization": bearer,
                    "Content-Type": "application/json",
                },
                b'{}',
            )
        )
        self.assertEqual(400, rolled_back.status)
        self.assertFalse(application.components.dashboard_push.tick())
        self.assertEqual(delivered, len(frames))

        self.assertIsNotNone(application.components.search_history_retention_job)
        application.start()
        names = {thread.name for thread in application.background_threads}
        application.stop()
        self.assertIn("vectorshelf-dashboard-push", names)
        self.assertIn("vectorshelf-search-history-retention", names)


if __name__ == "__main__":
    unittest.main()
