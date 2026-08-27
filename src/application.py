"""VectorShelf process composition root."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from threading import Event, RLock, Thread

from src.auth import AuthService, register_auth_routes
from src.collections import CollectionWorkspace, register_collection_routes
from src.documents import DocumentWorkspace, register_document_routes
from src.indexing import (
    ChunkProducer,
    EmbeddingProducer,
    IndexingService,
    register_indexing_routes,
)
from src.mcp import McpService
from src.ops import (
    DashboardBrokerPublisher,
    DashboardDestinationPolicy,
    DashboardPush,
    DashboardService,
    register_ops_routes,
)
from src.parsing import validate_chunk_config
from src.permissions import PermissionService, register_permission_routes
from src.platform import (
    InMemoryStompBroker,
    PlatformApp,
    SockJsHttpTransport,
    StompFrameProcessor,
    StompWebSocketGateway,
)
from src.search import SearchHistoryRetentionJob, SearchService
from src.shared import Request, Response
from src.sync import SyncDispatcher, SyncService, register_sync_routes
from src.users import UserDirectory, register_user_routes
from src.worker import WorkerConfig, WorkerRuntime


_LOG = logging.getLogger(__name__)

# PostgreSQL remains an explicit opt-in composition path.  Re-exporting the
# helpers here keeps this module as the public process composition root.
from src.application_postgres import (
    PostgresDomainComponents,
    PostgresLedgerAdapters,
    build_postgres_mcp_service,
    build_postgres_search_service,
    build_postgres_domain_components,
    build_postgres_ledger_adapters,
)


@dataclass(frozen=True, slots=True)
class ApplicationComponents:
    auth: AuthService
    users: UserDirectory
    documents: DocumentWorkspace
    collections: CollectionWorkspace
    permissions: PermissionService
    indexing: IndexingService
    search: SearchService
    sync: SyncService
    mcp: McpService
    ops: DashboardService
    chunk_producer: ChunkProducer
    embedder: EmbeddingProducer
    worker_runtime: WorkerRuntime | None = None
    sync_dispatcher: SyncDispatcher | None = None
    search_history_retention_job: SearchHistoryRetentionJob | None = None
    dashboard_push: DashboardPush | None = None
    stomp_broker: InMemoryStompBroker | None = None
    search_history_retention_interval_seconds: float = 86_400.0


class VectorShelfApplication:
    """HTTP/STOMP adapters and the process-owned background lifecycle."""

    def __init__(
        self,
        platform: PlatformApp,
        components: ApplicationComponents,
        stomp_broker: InMemoryStompBroker,
        stomp_processor: StompFrameProcessor,
        sockjs_transport: SockJsHttpTransport,
        websocket_gateway: StompWebSocketGateway,
    ) -> None:
        self.platform = platform
        self.app = platform
        self.components = components
        self.stomp_broker = stomp_broker
        self.stomp_processor = stomp_processor
        self.sockjs_transport = sockjs_transport
        self.websocket_gateway = websocket_gateway
        self._stop = Event()
        self._threads: list[Thread] = []
        self._lifecycle = RLock()
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        with self._lifecycle:
            return self._started

    @property
    def background_threads(self) -> tuple[Thread, ...]:
        with self._lifecycle:
            return tuple(self._threads)

    def handle(self, request: Request) -> Response:
        return self.platform.handle(request)

    def __call__(self, environ: dict[str, object], start_response):
        return self.platform(environ, start_response)

    def _supervise(
        self,
        name: str,
        target: object,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        restart_delay: float,
    ) -> None:
        while not self._stop.is_set():
            try:
                target(*args, **kwargs)  # type: ignore[operator]
                return
            except Exception as error:
                _LOG.error(
                    "background task failed task=%s exception=%s",
                    name,
                    type(error).__name__,
                )
            if self._stop.wait(restart_delay):
                return

    def start(self) -> None:
        with self._lifecycle:
            if self._started or self._closed:
                return
            worker = self.components.worker_runtime
            started_threads: list[Thread] = []
            try:
                if worker is not None:
                    worker.start()
                jobs: list[
                    tuple[str, object, tuple[object, ...], dict[str, object], float]
                ] = []
                if self.components.sync_dispatcher is not None:
                    poll_interval = getattr(
                        getattr(self.components.sync_dispatcher, "service", None),
                        "poll_interval",
                        None,
                    )
                    jobs.append((
                        "vectorshelf-sync",
                        self.components.sync_dispatcher.run,
                        (self._stop,),
                        {},
                        float(poll_interval.total_seconds())
                        if hasattr(poll_interval, "total_seconds")
                        else 1.0,
                    ))
                if self.components.search_history_retention_job is not None:
                    jobs.append((
                        "vectorshelf-search-history-retention",
                        self.components.search_history_retention_job.serve,
                        (self._stop,),
                        {
                            "interval_seconds": self.components.search_history_retention_interval_seconds
                        },
                        self.components.search_history_retention_interval_seconds,
                    ))
                if self.components.dashboard_push is not None:
                    jobs.append((
                        "vectorshelf-dashboard-push",
                        self.components.dashboard_push.run,
                        (self._stop,),
                        {},
                        float(getattr(self.components.dashboard_push, "debounce_seconds", 0.3)),
                    ))
                for name, target, args, kwargs, restart_delay in jobs:
                    thread = Thread(
                        target=self._supervise,
                        args=(name, target, args, kwargs, restart_delay),
                        name=name,
                        daemon=True,
                    )
                    started_threads.append(thread)
                    thread.start()
            except Exception:
                self._stop.set()
                for thread in started_threads:
                    thread.join(timeout=1.0)
                if worker is not None:
                    worker.shutdown()
                self._closed = True
                raise
            self._threads.extend(started_threads)
            self._started = True

    def stop(self) -> None:
        with self._lifecycle:
            if not self._started:
                return
            self._stop.set()
            for thread in self._threads:
                thread.join(timeout=1.0)
            if self.components.worker_runtime is not None:
                self.components.worker_runtime.shutdown()
            self._started = False
            self._closed = True


def _number(
    settings: Mapping[str, object], key: str, default: float
) -> float:
    value = settings.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _integer(settings: Mapping[str, object], key: str, default: int) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _boolean(settings: Mapping[str, object], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _worker_config(settings: Mapping[str, object]) -> WorkerConfig:
    enabled = _boolean(settings, "indexing.worker.enabled", False)
    name = settings.get("indexing.worker.name", "indexing-worker")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("indexing.worker.name must be non-blank")
    return WorkerConfig(
        enabled=enabled,
        name=name,
        heartbeat_interval=_number(
            settings, "indexing.worker.heartbeat-interval", 10
        ),
        dead_threshold=_number(settings, "indexing.worker.dead-threshold", 30),
        poll_interval=_number(settings, "indexing.worker.poll-interval", 1),
        empty_poll_max=_number(settings, "indexing.worker.empty-poll-max", 10),
        max_concurrency=_integer(settings, "indexing.worker.max-concurrency", 2),
        lease_duration=_number(settings, "indexing.worker.lease-duration", 300),
        lease_renew_interval=_number(
            settings, "indexing.worker.lease-renew-interval", 60
        ),
        recovery_interval=_number(
            settings, "indexing.worker.recovery-interval", 30
        ),
        recovery_batch_size=_integer(
            settings, "indexing.worker.recovery-batch-size", 100
        ),
        shutdown_grace=_number(settings, "indexing.worker.shutdown-grace", 30),
    )


def _validate_parser_handlers(settings: Mapping[str, object]) -> None:
    configured = settings.get(
        "document.parsing.handlers", ("PDF", "DOCX", "TXT", "MD")
    )
    if isinstance(configured, Mapping):
        formats = tuple(str(item).strip().upper() for item in configured)
    elif isinstance(configured, (list, tuple, set, frozenset)):
        formats = tuple(
            str(
                item[0]
                if isinstance(item, (list, tuple)) and item
                else ""
                if isinstance(item, (list, tuple))
                else item
            )
            .strip()
            .upper()
            for item in configured
        )
    else:
        raise ValueError("document.parsing.handlers must be a handler registry")
    if not formats or any(not item for item in formats) or len(formats) != len(set(formats)):
        raise ValueError("document parser handlers must be non-empty and unique by format")


def _validate_settings(settings: Mapping[str, object]) -> WorkerConfig:
    secret = settings.get("jwt.secret")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("jwt.secret is required")
    storage_type = settings.get("storage.type", "local")
    if storage_type not in {"local", "minio", "s3"}:
        raise ValueError("storage.type must be local, minio, or s3")
    if storage_type in {"minio", "s3"}:
        bucket = settings.get("storage.bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("external storage bucket/namespace is required")
    if _integer(settings, "jwt.expiration", 3600) <= 0:
        raise ValueError("jwt.expiration must be positive")

    validate_chunk_config(
        _integer(settings, "document.chunking.chunk-size", 1000),
        _integer(settings, "document.chunking.overlap", 200),
    )
    worker_config = _worker_config(settings)
    retry_initial = _number(settings, "indexing.worker.retry-initial", 10)
    retry_max = _number(settings, "indexing.worker.retry-max", 300)
    if retry_initial <= 0 or retry_max < retry_initial:
        raise ValueError("indexing worker retry delay is invalid")
    retry_jitter = _number(settings, "indexing.worker.retry-jitter", 0.2)
    if not 0 <= retry_jitter <= 1:
        raise ValueError("indexing worker retry jitter must be between zero and one")
    if _number(settings, "embedding.circuit-open-seconds", 30) <= 0:
        raise ValueError("embedding circuit open time must be positive")
    if not 1 <= _integer(settings, "embedding.batch-size", 4) <= 64:
        raise ValueError("embedding.batch-size must be between 1 and 64")
    if min(
        _number(settings, "embedding.connect-timeout", 5),
        _number(settings, "embedding.response-timeout", 5),
        _number(settings, "embedding.document-batch-response-timeout", 30),
    ) <= 0:
        raise ValueError("embedding timeouts must be positive")
    _boolean(settings, "embedding.circuit-enabled", True)
    if min(
        _integer(settings, "embedding.character-budget", 4000),
        _integer(settings, "embedding.token-budget", 900),
        _integer(settings, "embedding.circuit-failure-threshold", 5),
    ) <= 0:
        raise ValueError("embedding budgets and circuit threshold must be positive")
    if _integer(settings, "indexing.job.max-retries", 3) < 0:
        raise ValueError("indexing job max retries must not be negative")
    _integer(settings, "indexing.job.default-priority", 0)

    sync_poll = _number(settings, "sync.dispatcher.poll-interval", 1)
    sync_lease = _number(settings, "sync.dispatcher.lease-duration", 30)
    sync_recovery = _number(settings, "sync.dispatcher.recovery-interval", 10)
    sync_retry_initial = _number(settings, "sync.dispatcher.retry-initial", 5)
    sync_retry_max = _number(settings, "sync.dispatcher.retry-max", 60)
    if min(sync_poll, sync_lease, sync_recovery, sync_retry_initial) <= 0:
        raise ValueError("sync dispatcher periods must be positive")
    if sync_retry_max < sync_retry_initial:
        raise ValueError("sync dispatcher retry delay is invalid")
    if _integer(settings, "sync.dispatcher.recovery-batch", 100) <= 0:
        raise ValueError("sync dispatcher recovery batch must be positive")
    if _integer(settings, "sync.dispatcher.max-retries", 5) < 0:
        raise ValueError("sync dispatcher max retries must not be negative")
    _boolean(settings, "sync.dispatcher.enabled", False)
    _boolean(settings, "sync.reconciliation.enabled", False)
    if min(
        _number(settings, "sync.reconciliation.period", 300),
        _number(settings, "sync.reconciliation.stalled-threshold", 900),
    ) <= 0:
        raise ValueError("sync reconciliation periods must be positive")
    if _integer(settings, "sync.reconciliation.batch", 100) <= 0:
        raise ValueError("sync reconciliation batch must be positive")
    if settings.get("sync.reconciliation.mode", "DRY_RUN") not in {
        "DRY_RUN",
        "REPAIR",
    }:
        raise ValueError("sync reconciliation mode is invalid")
    if _number(settings, "dashboard.push-debounce", 0.3) <= 0:
        raise ValueError("dashboard push debounce must be positive")
    if _integer(settings, "scheduler.background-worker-pool", 4) <= 0:
        raise ValueError("scheduler background worker pool must be positive")
    _validate_parser_handlers(settings)
    return worker_config


def validate_settings(settings: Mapping[str, object]) -> WorkerConfig:
    """Validate process configuration before any deployment adapter is opened."""

    return _validate_settings(settings)


def _worker_runtime(
    settings: Mapping[str, object],
    components: ApplicationComponents,
    config: WorkerConfig,
) -> WorkerRuntime:
    indexing = components.indexing
    indexing.lease_duration = timedelta(seconds=config.lease_duration)
    indexing.dead_threshold = timedelta(seconds=config.dead_threshold)
    indexing.retry_initial = timedelta(
        seconds=_number(settings, "indexing.worker.retry-initial", 10)
    )
    indexing.retry_max = timedelta(
        seconds=_number(settings, "indexing.worker.retry-max", 300)
    )
    indexing.retry_jitter = _number(settings, "indexing.worker.retry-jitter", 0.2)

    def execute_claim(claim: Mapping[str, object], interrupted: Event) -> None:
        job_id = int(claim["jobId"])
        worker_id = int(claim["workerId"])
        token = str(claim["claimToken"])
        attempt_id: int | None = None
        try:
            attempt = indexing.start_attempt(job_id, worker_id, token)
            if attempt.data is None:
                raise RuntimeError("attempt response has no data")
            attempt_id = int(attempt.data["attemptId"])
            if interrupted.is_set():
                return
            indexing.save_chunks(
                job_id,
                attempt_id,
                worker_id,
                token,
                creator=lambda: components.chunk_producer(
                    job_id, attempt_id, worker_id, token
                ),
            )
            if interrupted.is_set():
                return
            indexing.save_embeddings(
                job_id,
                attempt_id,
                worker_id,
                token,
                components.embedder,
            )
            if interrupted.is_set():
                return
            indexing.complete(job_id, attempt_id, worker_id, token)
        except Exception as error:
            if attempt_id is None or interrupted.is_set():
                raise
            indexing.report_failure_from_worker(
                job_id,
                attempt_id,
                worker_id,
                token,
                "WORKER_INTERNAL_ERROR",
                type(error).__name__,
            )

    return WorkerRuntime(indexing, execute_claim, config)


def create_application(
    settings: Mapping[str, object], components: ApplicationComponents
) -> VectorShelfApplication:
    """Validate startup settings, mount every feature, and retain runtime adapters."""

    worker_config = validate_settings(settings)
    if components.search_history_retention_interval_seconds <= 0:
        raise ValueError("search history retention interval must be positive")

    components.permissions.bind_catalogs(
        components.documents, components.collections
    )
    components.documents.bind_indexing(components.indexing)
    components.documents.bind_sync_outbox(components.sync)
    components.permissions.bind_sync_outbox(components.sync)
    components.documents.bind_permissions(components.permissions)
    components.collections.bind_permissions(components.permissions)

    supplied_push = components.dashboard_push
    supplied_publisher = getattr(supplied_push, "publisher", None)
    supplied_broker = getattr(supplied_publisher, "broker", None)
    broker = components.stomp_broker or supplied_broker or InMemoryStompBroker()
    if supplied_broker is not None and supplied_broker is not broker:
        raise ValueError("dashboard push and STOMP processor must share one broker")
    dashboard_push = supplied_push or DashboardPush(
        components.ops,
        DashboardBrokerPublisher(broker),
        debounce_seconds=_number(settings, "dashboard.push-debounce", 0.3),
    )
    retention = components.search_history_retention_job or SearchHistoryRetentionJob(
        components.search.ports.history
    )
    worker_runtime = components.worker_runtime
    if "indexing.worker.enabled" in settings and not worker_config.enabled:
        worker_runtime = None
    elif worker_runtime is None and worker_config.enabled:
        worker_runtime = _worker_runtime(settings, components, worker_config)
    sync_dispatcher = components.sync_dispatcher
    if not (
        _boolean(settings, "sync.dispatcher.enabled", False)
        or _boolean(settings, "sync.reconciliation.enabled", False)
    ):
        sync_dispatcher = None
    runtime_components = replace(
        components,
        worker_runtime=worker_runtime,
        sync_dispatcher=sync_dispatcher,
        dashboard_push=dashboard_push,
        search_history_retention_job=retention,
        stomp_broker=broker,
    )
    if runtime_components.worker_runtime is not None:
        bind_worker_listener = getattr(
            runtime_components.worker_runtime, "bind_state_transition_listener", None
        )
        if callable(bind_worker_listener):
            bind_worker_listener(dashboard_push.state_transition_committed)
    if runtime_components.sync_dispatcher is not None:
        bind_sync_listener = getattr(
            runtime_components.sync_dispatcher, "bind_state_transition_listener", None
        )
        if callable(bind_sync_listener):
            bind_sync_listener(dashboard_push.state_transition_committed)

    platform = PlatformApp(
        components.auth.resolve_request,
        state_transition_committed=dashboard_push.state_transition_committed,
    )
    register_auth_routes(platform, components.auth)
    register_user_routes(platform, components.users)
    register_document_routes(platform, components.documents)
    register_collection_routes(platform, components.collections)
    register_permission_routes(
        platform, components.permissions, user_search=components.users.search_users
    )
    register_indexing_routes(
        platform,
        components.indexing,
        chunk_producer=components.chunk_producer,
        embedder=components.embedder,
    )
    components.search.mount(platform)
    register_sync_routes(platform, components.sync)
    components.mcp.mount(platform)
    register_ops_routes(platform, components.ops, dashboard_push)

    processor = StompFrameProcessor(
        components.auth.resolve_request,
        DashboardDestinationPolicy(),
        broker,
    )
    sockjs = SockJsHttpTransport(processor)
    sockjs.mount(platform)
    return VectorShelfApplication(
        platform,
        runtime_components,
        broker,
        processor,
        sockjs,
        StompWebSocketGateway(processor),
    )
