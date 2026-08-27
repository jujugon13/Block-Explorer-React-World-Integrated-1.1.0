"""Production composition root for the VectorShelf ASGI process."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Callable

from fastapi import FastAPI

from src.application import ApplicationComponents, VectorShelfApplication, create_application, validate_settings
from src.application_postgres import (
    build_postgres_domain_components,
    build_postgres_ledger_adapters,
    build_postgres_mcp_service,
    build_postgres_search_service,
)
from src.auth import AuthService, TokenManager
from src.guardrails import GuardrailService
from src.infra.ai import build_ai_adapters, validate_ai_configuration
from src.infra.cache import RedisConfig, build_redis_cache
from src.infra.http import create_fastapi_app
from src.infra.postgres import PostgresConfig
from src.infra.s3.adapter import S3Config, build_s3_storage
from src.mcp import McpApplicationBackend
from src.ops import CompositeOpsSnapshotReader, DashboardService
from src.parsing import chunk_sections, parse_document
from src.search import SearchPorts
from src.shared import ObjectStorage, Principal
from src.storage import select_storage

from .settings import load_settings


class RuntimeConfigurationError(ValueError):
    """The selected deployment combination has no production adapter."""


def _boolean(settings: Mapping[str, object], key: str) -> bool:
    value = settings.get(key)
    if not isinstance(value, bool):
        raise RuntimeConfigurationError(f"{key} must be boolean")
    return value


def _number(settings: Mapping[str, object], key: str) -> float:
    value = settings.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeConfigurationError(f"{key} must be numeric")
    return float(value)


def _integer(settings: Mapping[str, object], key: str) -> int:
    value = settings.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeConfigurationError(f"{key} must be an integer")
    return value


def _storage(
    settings: Mapping[str, object], s3_config: S3Config | None = None
) -> ObjectStorage:
    provider = settings.get("storage.type")
    if provider == "local":
        return select_storage(settings)
    if provider == "s3":
        bucket = settings.get("storage.bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            raise RuntimeConfigurationError("storage.bucket is required for s3")
        return build_s3_storage(s3_config or S3Config(bucket))
    if provider == "minio":
        raise RuntimeConfigurationError(
            "storage.type=minio has no configured production client"
        )
    raise RuntimeConfigurationError("storage.type must be local, minio, or s3")


def _indexing_options(settings: Mapping[str, object]) -> dict[str, object]:
    return {
        "lease_duration": timedelta(seconds=_number(settings, "indexing.worker.lease-duration")),
        "dead_threshold": timedelta(seconds=_number(settings, "indexing.worker.dead-threshold")),
        "retry_initial": timedelta(seconds=_number(settings, "indexing.worker.retry-initial")),
        "retry_max": timedelta(seconds=_number(settings, "indexing.worker.retry-max")),
        "retry_jitter": _number(settings, "indexing.worker.retry-jitter"),
        "default_job_max_retries": _integer(settings, "indexing.job.max-retries"),
        "default_job_priority": _integer(settings, "indexing.job.default-priority"),
    }


def _sync_options(settings: Mapping[str, object]) -> dict[str, object]:
    return {
        "dispatcher_enabled": _boolean(settings, "sync.dispatcher.enabled"),
        "dispatcher_name": str(settings["sync.dispatcher.name"]),
        "poll_interval": timedelta(seconds=_number(settings, "sync.dispatcher.poll-interval")),
        "lease_duration": timedelta(seconds=_number(settings, "sync.dispatcher.lease-duration")),
        "recovery_interval": timedelta(seconds=_number(settings, "sync.dispatcher.recovery-interval")),
        "recovery_batch": _integer(settings, "sync.dispatcher.recovery-batch"),
        "retry_initial": timedelta(seconds=_number(settings, "sync.dispatcher.retry-initial")),
        "retry_max": timedelta(seconds=_number(settings, "sync.dispatcher.retry-max")),
        "default_max_retries": _integer(settings, "sync.dispatcher.max-retries"),
        "reconciliation_enabled": _boolean(settings, "sync.reconciliation.enabled"),
        "reconciliation_mode": str(settings["sync.reconciliation.mode"]),
        "reconciliation_batch": _integer(settings, "sync.reconciliation.batch"),
        "reconciliation_interval": timedelta(seconds=_number(settings, "sync.reconciliation.period")),
        "stalled_threshold": timedelta(seconds=_number(settings, "sync.reconciliation.stalled-threshold")),
    }


def _chunk_producer(
    indexing: object,
    document_store: object,
    storage: ObjectStorage,
    settings: Mapping[str, object],
) -> Callable[[int, int, int, str], tuple[object, ...]]:
    """Fetch and parse a claimed version; IndexingService owns all state writes."""

    chunk_size = _integer(settings, "document.chunking.chunk-size")
    overlap = _integer(settings, "document.chunking.overlap")

    def produce(job_id: int, _attempt_id: int, _worker_id: int, _token: str) -> tuple[object, ...]:
        detail = getattr(indexing, "detail")(job_id)
        version_id = detail.get("documentVersionId") if isinstance(detail, Mapping) else None
        if isinstance(version_id, bool) or not isinstance(version_id, int):
            raise RuntimeError("indexing job has no document version")
        version = getattr(document_store, "version")(version_id)
        if version is None:
            raise RuntimeError("document version is unavailable")
        file = getattr(document_store, "file")(version.file_object_id)
        if file is None:
            raise RuntimeError("document file is unavailable")
        sections = parse_document(storage.get(file.location), file.document_type)
        return tuple(chunk_sections(sections, chunk_size, overlap))

    return produce


def _principal_factory(users: object) -> Callable[[int], Principal | None]:
    def principal_for(user_id: int) -> Principal | None:
        user = getattr(users, "get_user")(user_id)
        if user is None:
            return None
        return Principal(
            user.email,
            frozenset(getattr(users, "roles_for")(user.id)),
            user_id=user.id,
            department_id=user.department_id,
            display_name=user.name,
        )

    return principal_for


def build_application(settings: Mapping[str, object]) -> VectorShelfApplication:
    """Assemble every real adapter before exposing HTTP or background work."""

    validate_settings(settings)
    postgres_config = PostgresConfig.from_env()
    cache_config = RedisConfig.from_env()
    validate_ai_configuration()
    s3_config = (
        S3Config(str(settings["storage.bucket"]))
        if settings.get("storage.type") == "s3"
        else None
    )
    storage = _storage(settings, s3_config)
    adapters = build_postgres_ledger_adapters(postgres_config)
    domain = build_postgres_domain_components(
        storage,
        adapters,
        indexing_options=_indexing_options(settings),
        sync_options=_sync_options(settings),
        orphan_grace=timedelta(seconds=_number(settings, "sync.reconciliation.orphan-grace")),
    )
    ai = build_ai_adapters(settings=settings)
    cache = build_redis_cache(cache_config)
    secret = settings.get("jwt.secret")
    if not isinstance(secret, str):
        raise RuntimeConfigurationError("jwt.secret is required")
    auth = AuthService(
        domain.users,
        cache,
        TokenManager(secret, _integer(settings, "jwt.expiration")),
    )
    search = build_postgres_search_service(
        SearchPorts(
            domain.indexing,
            domain.permissions,
            ai.query_embedder,
            adapters.vector_searcher,
            adapters.keyword_searcher,
            ai.llm,
            GuardrailService(),
            adapters.search_history_store,
            cache=cache,
            reranker=ai.reranker,
        ),
        adapters,
        stored_settings=settings,
    )
    mcp = build_postgres_mcp_service(
        McpApplicationBackend(search, domain.documents, domain.permissions),
        adapters,
        principal_factory=_principal_factory(domain.users),
    )
    ops = DashboardService(
        CompositeOpsSnapshotReader(
            domain.documents,
            domain.indexing,
            adapters.search_history_store,
        ),
        worker_dead_threshold=timedelta(seconds=_number(settings, "indexing.worker.dead-threshold")),
        indexing_commands=domain.indexing,
    )
    dispatcher = (
        domain.sync_dispatcher
        if _boolean(settings, "sync.dispatcher.enabled")
        or _boolean(settings, "sync.reconciliation.enabled")
        else None
    )
    components = ApplicationComponents(
        auth=auth,
        users=domain.users,
        documents=domain.documents,
        collections=domain.collections,
        permissions=domain.permissions,
        indexing=domain.indexing,
        search=search,
        sync=domain.sync,
        mcp=mcp,
        ops=ops,
        chunk_producer=_chunk_producer(
            domain.indexing, adapters.document_store, storage, settings
        ),
        embedder=ai.embed_documents,
        sync_dispatcher=dispatcher,
    )
    return create_application(settings, components)


def create_asgi_application(
    settings: Mapping[str, object] | None = None,
) -> FastAPI:
    """Build the real FastAPI app and bind the application lifecycle exactly once."""

    selected = dict(settings) if settings is not None else load_settings()
    application = build_application(selected)
    return create_fastapi_app(
        application.handle,
        application.websocket_gateway,
        startup=application.start,
        shutdown=application.stop,
    )
