"""PostgreSQL ledger and domain composition helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from typing import Any, cast

from src.collections import CollectionWorkspace
from src.documents import DocumentWorkspace
from src.indexing import IndexingService
from src.infra.ai import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_VERSION,
    EMBEDDING_PROVIDER,
)
from src.infra.postgres.collection_store import PostgresCollectionStore
from src.infra.postgres.config import PostgresConfig, connect
from src.infra.postgres.document_store import PostgresDocumentStore
from src.infra.postgres.identity_store import PostgresIdentityStore
from src.infra.postgres.indexing_store import PostgresIndexingStore
from src.infra.postgres.mcp_store import PostgresMcpTokenStore
from src.infra.postgres.migrate import MigrationReport, run_migrations
from src.infra.postgres.permission_store import PostgresPermissionStore
from src.infra.postgres.preflight import (
    PostgresCapabilities,
    verify_postgres_capabilities,
)
from src.infra.postgres.search_history_store import PostgresSearchHistoryStore
from src.infra.postgres.sync_store import PostgresSyncStore
from src.infra.postgres.transaction import PostgresTransactionManager
from src.infra.search import PostgresKeywordSearcher, PostgresVectorSearcher
from src.permissions import PermissionService
from src.mcp import McpService
from src.search import SearchPorts, SearchService
from src.shared import McpToolBackend, ObjectInventory, ObjectStorage
from src.sync import (
    SyncDispatcher,
    SyncHandlerRegistry,
    SyncService,
    indexing_handlers,
)
from src.sync.consistency import OrphanedDataDetector
from src.users import UserDirectory


ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class PostgresLedgerAdapters:
    """Stores sharing one ambient PostgreSQL transaction manager."""

    config: PostgresConfig
    transactions: PostgresTransactionManager
    migration_report: MigrationReport | None
    capabilities: PostgresCapabilities | None
    identity_store: PostgresIdentityStore
    document_store: PostgresDocumentStore
    collection_store: PostgresCollectionStore
    permission_store: PostgresPermissionStore
    indexing_store: PostgresIndexingStore
    sync_store: PostgresSyncStore
    mcp_store: PostgresMcpTokenStore
    search_history_store: PostgresSearchHistoryStore
    vector_searcher: PostgresVectorSearcher
    keyword_searcher: PostgresKeywordSearcher


@dataclass(frozen=True, slots=True)
class PostgresDomainComponents:
    """Domain services whose mutable ledgers live in PostgreSQL."""

    users: UserDirectory
    indexing: IndexingService
    sync: SyncService
    documents: DocumentWorkspace
    collections: CollectionWorkspace
    permissions: PermissionService
    sync_handlers: SyncHandlerRegistry
    sync_dispatcher: SyncDispatcher


def build_postgres_ledger_adapters(
    config: PostgresConfig | None = None,
    connection_factory: ConnectionFactory | None = None,
    apply_migrations: bool = True,
    verify: bool = True,
) -> PostgresLedgerAdapters:
    """Fail closed while assembling every PostgreSQL ledger store."""

    selected_config = config if config is not None else PostgresConfig.from_env()
    factory = (
        connection_factory
        if connection_factory is not None
        else partial(connect, selected_config)
    )
    migration_report = (
        run_migrations(selected_config, connection_factory=factory)
        if apply_migrations
        else None
    )
    transactions = PostgresTransactionManager(factory)
    capabilities = verify_postgres_capabilities(transactions) if verify else None

    return PostgresLedgerAdapters(
        config=selected_config,
        transactions=transactions,
        migration_report=migration_report,
        capabilities=capabilities,
        identity_store=PostgresIdentityStore(transactions),
        document_store=PostgresDocumentStore(transactions),
        collection_store=PostgresCollectionStore(transactions),
        permission_store=PostgresPermissionStore(transactions),
        indexing_store=PostgresIndexingStore(transactions),
        sync_store=PostgresSyncStore(transactions),
        mcp_store=PostgresMcpTokenStore(transactions),
        search_history_store=PostgresSearchHistoryStore(transactions),
        vector_searcher=PostgresVectorSearcher(transactions),
        keyword_searcher=PostgresKeywordSearcher(transactions),
    )


def build_postgres_domain_components(
    storage: ObjectStorage,
    adapters: PostgresLedgerAdapters,
    *,
    clock: Callable[..., Any] | None = None,
    indexing_options: Mapping[str, object] | None = None,
    sync_options: Mapping[str, object] | None = None,
    orphan_grace: timedelta = timedelta(hours=24),
) -> PostgresDomainComponents:
    """Inject PostgreSQL stores before any cross-domain binding occurs."""

    indexing_arguments = dict(indexing_options or {})
    sync_arguments = dict(sync_options or {})
    if clock is not None:
        indexing_arguments["clock"] = clock
        sync_arguments["clock"] = clock

    users = UserDirectory(store=adapters.identity_store)
    indexing = IndexingService(adapters.indexing_store, **indexing_arguments)
    indexing.ensure_embedding_model(
        provider=EMBEDDING_PROVIDER,
        model_name=EMBEDDING_MODEL,
        model_version=EMBEDDING_MODEL_VERSION,
        dimension=EMBEDDING_DIMENSION,
    )
    inventory = (
        cast(ObjectInventory, storage)
        if callable(getattr(storage, "iter_objects", None))
        else None
    )
    detector = (
        OrphanedDataDetector(
            inventory,
            adapters.document_store,
            clock=clock,
            grace=orphan_grace,
        )
        if inventory is not None
        else None
    )
    sync = SyncService(adapters.sync_store, detector=detector, **sync_arguments)
    documents = DocumentWorkspace(
        storage,
        store=adapters.document_store,
        indexing=indexing,
        sync_outbox=sync,
        clock=clock,
    )
    collections = CollectionWorkspace(
        documents=documents,
        store=adapters.collection_store,
    )
    permissions = PermissionService(
        documents,
        collections,
        store=adapters.permission_store,
        sync_outbox=sync,
        clock=clock,
    )
    documents.bind_permissions(permissions)
    collections.bind_permissions(permissions)
    handlers = indexing_handlers(indexing, documents, permissions)
    dispatcher = SyncDispatcher(sync, handlers)
    return PostgresDomainComponents(
        users,
        indexing,
        sync,
        documents,
        collections,
        permissions,
        handlers,
        dispatcher,
    )


def build_postgres_mcp_service(
    backend: McpToolBackend,
    adapters: PostgresLedgerAdapters,
    **options: Any,
) -> McpService:
    """Bind the PostgreSQL token ledger without guessing MCP dependencies."""

    return McpService(backend, token_store=adapters.mcp_store, **options)


def build_postgres_search_service(
    ports: SearchPorts,
    adapters: PostgresLedgerAdapters,
    **options: Any,
) -> SearchService:
    """Bind PostgreSQL search and history while callers supply AI ports."""

    return SearchService(
        replace(
            ports,
            vector=adapters.vector_searcher,
            keyword=adapters.keyword_searcher,
            history=adapters.search_history_store,
        ),
        **options,
    )
