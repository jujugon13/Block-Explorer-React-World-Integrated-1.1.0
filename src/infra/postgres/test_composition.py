"""Offline composition tests; no test in this module opens PostgreSQL."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import src.application_postgres as composition
from src.storage import select_storage


_STORE_TYPES = (
    "PostgresIdentityStore",
    "PostgresDocumentStore",
    "PostgresCollectionStore",
    "PostgresPermissionStore",
    "PostgresIndexingStore",
    "PostgresSyncStore",
    "PostgresMcpTokenStore",
    "PostgresSearchHistoryStore",
)
_STORE_FIELDS = (
    "identity_store",
    "document_store",
    "collection_store",
    "permission_store",
    "indexing_store",
    "sync_store",
    "mcp_store",
    "search_history_store",
)


class _CapturedStore:
    def __init__(self, transactions: object) -> None:
        self.transactions = transactions


class PostgresCompositionTests(unittest.TestCase):
    def test_IT_COMPOSITION_001_migration_precedes_preflight_and_stores_share_uow(
        self,
    ) -> None:
        events: list[str] = []
        config = object()
        factory = Mock(name="connection_factory")
        manager = object()
        migration_report = object()
        capabilities = object()

        def migrate(*args, **kwargs):
            events.append("migration")
            self.assertEqual((config,), args)
            self.assertIs(factory, kwargs["connection_factory"])
            return migration_report

        def make_manager(received_factory):
            events.append("manager")
            self.assertIs(factory, received_factory)
            return manager

        def preflight(received_manager):
            events.append("preflight")
            self.assertIs(manager, received_manager)
            return capabilities

        from_env_patch = patch.object(
            composition.PostgresConfig, "from_env", return_value=config
        )
        patches = [
            from_env_patch,
            patch.object(composition, "run_migrations", side_effect=migrate),
            patch.object(
                composition,
                "PostgresTransactionManager",
                side_effect=make_manager,
            ),
            patch.object(
                composition,
                "verify_postgres_capabilities",
                side_effect=preflight,
            ),
        ]
        store_mocks: list[Mock] = []
        for name in _STORE_TYPES:
            store_type = Mock(name=name, side_effect=_CapturedStore)
            store_mocks.append(store_type)
            patches.append(patch.object(composition, name, store_type))

        with ExitStack() as stack:
            entered = [stack.enter_context(patcher) for patcher in patches]
            adapters = composition.build_postgres_ledger_adapters(
                connection_factory=factory
            )

        entered[0].assert_called_once_with()
        self.assertEqual(["migration", "manager", "preflight"], events)
        self.assertIs(config, adapters.config)
        self.assertIs(manager, adapters.transactions)
        self.assertIs(migration_report, adapters.migration_report)
        self.assertIs(capabilities, adapters.capabilities)
        for field, store_type in zip(_STORE_FIELDS, store_mocks, strict=True):
            store_type.assert_called_once_with(manager)
            self.assertIs(manager, getattr(adapters, field).transactions)
        self.assertIs(manager, adapters.vector_searcher.transactions)
        self.assertIs(manager, adapters.keyword_searcher.transactions)

    def test_IT_COMPOSITION_002_startup_failure_has_no_in_memory_fallback(
        self,
    ) -> None:
        failure = RuntimeError("offline migration failed")
        manager_type = Mock(name="PostgresTransactionManager")
        with (
            patch.object(composition, "run_migrations", side_effect=failure),
            patch.object(composition, "PostgresTransactionManager", manager_type),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline migration failed"):
                composition.build_postgres_ledger_adapters(
                    config=object(),
                    connection_factory=Mock(name="connection_factory"),
                )
        manager_type.assert_not_called()

    def test_IT_COMPOSITION_003_local_storage_skips_orphan_inventory(
        self,
    ) -> None:
        adapters = SimpleNamespace(
            identity_store=object(),
            indexing_store=object(),
            sync_store=object(),
            document_store=object(),
            collection_store=object(),
            permission_store=object(),
        )
        storage = select_storage({"storage.type": "local"})
        clock = Mock(name="clock")
        users = object()
        indexing = Mock(name="indexing")
        sync = object()
        documents = Mock(name="documents")
        collections = Mock(name="collections")
        permissions = object()
        handlers = object()
        dispatcher = object()
        order: list[str] = []

        def constructed(name: str, value: object):
            def factory(*args, **kwargs):
                del args, kwargs
                order.append(name)
                return value

            return factory

        constructors = {
            "UserDirectory": Mock(side_effect=constructed("users", users)),
            "IndexingService": Mock(side_effect=constructed("indexing", indexing)),
            "SyncService": Mock(side_effect=constructed("sync", sync)),
            "DocumentWorkspace": Mock(
                side_effect=constructed("documents", documents)
            ),
            "CollectionWorkspace": Mock(
                side_effect=constructed("collections", collections)
            ),
            "PermissionService": Mock(
                side_effect=constructed("permissions", permissions)
            ),
            "indexing_handlers": Mock(side_effect=constructed("handlers", handlers)),
            "SyncDispatcher": Mock(
                side_effect=constructed("dispatcher", dispatcher)
            ),
        }
        with (
            patch.object(composition, "UserDirectory", constructors["UserDirectory"]),
            patch.object(
                composition, "IndexingService", constructors["IndexingService"]
            ),
            patch.object(composition, "SyncService", constructors["SyncService"]),
            patch.object(
                composition,
                "DocumentWorkspace",
                constructors["DocumentWorkspace"],
            ),
            patch.object(
                composition,
                "CollectionWorkspace",
                constructors["CollectionWorkspace"],
            ),
            patch.object(
                composition, "PermissionService", constructors["PermissionService"]
            ),
            patch.object(
                composition, "indexing_handlers", constructors["indexing_handlers"]
            ),
            patch.object(
                composition, "SyncDispatcher", constructors["SyncDispatcher"]
            ),
        ):
            result = composition.build_postgres_domain_components(
                storage,
                adapters,
                clock=clock,
                indexing_options={"lease_duration": "lease"},
                sync_options={"dispatcher_enabled": True},
            )

        self.assertEqual(
            [
                "users",
                "indexing",
                "sync",
                "documents",
                "collections",
                "permissions",
                "handlers",
                "dispatcher",
            ],
            order,
        )
        constructors["UserDirectory"].assert_called_once_with(
            store=adapters.identity_store
        )
        constructors["IndexingService"].assert_called_once_with(
            adapters.indexing_store,
            lease_duration="lease",
            clock=clock,
        )
        indexing.ensure_embedding_model.assert_called_once_with(
            provider="OPENAI",
            model_name="text-embedding-3-small",
            model_version="text-embedding-3-small",
            dimension=1536,
        )
        constructors["SyncService"].assert_called_once()
        sync_args, sync_kwargs = constructors["SyncService"].call_args
        self.assertEqual((adapters.sync_store,), sync_args)
        self.assertIsNone(sync_kwargs.pop("detector"))
        self.assertEqual(
            {"dispatcher_enabled": True, "clock": clock},
            sync_kwargs,
        )
        constructors["DocumentWorkspace"].assert_called_once_with(
            storage,
            store=adapters.document_store,
            indexing=indexing,
            sync_outbox=sync,
            clock=clock,
        )
        constructors["CollectionWorkspace"].assert_called_once_with(
            documents=documents,
            store=adapters.collection_store,
        )
        constructors["PermissionService"].assert_called_once_with(
            documents,
            collections,
            store=adapters.permission_store,
            sync_outbox=sync,
            clock=clock,
        )
        documents.bind_permissions.assert_called_once_with(permissions)
        collections.bind_permissions.assert_called_once_with(permissions)
        constructors["indexing_handlers"].assert_called_once_with(
            indexing, documents, permissions
        )
        constructors["SyncDispatcher"].assert_called_once_with(sync, handlers)
        self.assertEqual(
            (
                users,
                indexing,
                sync,
                documents,
                collections,
                permissions,
                handlers,
                dispatcher,
            ),
            (
                result.users,
                result.indexing,
                result.sync,
                result.documents,
                result.collections,
                result.permissions,
                result.sync_handlers,
                result.sync_dispatcher,
            ),
        )

    def test_IT_COMPOSITION_004_mcp_and_search_consume_postgres_stores(self) -> None:
        adapters = SimpleNamespace(
            mcp_store=object(),
            search_history_store=object(),
            vector_searcher=object(),
            keyword_searcher=object(),
        )
        backend = object()
        ports = SimpleNamespace(history=object())
        mcp = object()
        search = object()
        with (
            patch.object(composition, "McpService", return_value=mcp) as mcp_type,
            patch.object(
                composition, "SearchService", return_value=search
            ) as search_type,
            patch.object(
                composition, "replace", return_value="postgres-search-ports"
            ) as replace_ports,
        ):
            built_mcp = composition.build_postgres_mcp_service(
                backend, adapters, clock="clock"
            )
            built_search = composition.build_postgres_search_service(
                ports, adapters, stored_settings={"search_mode": "hybrid"}
            )

        self.assertIs(mcp, built_mcp)
        self.assertIs(search, built_search)
        mcp_type.assert_called_once_with(
            backend, token_store=adapters.mcp_store, clock="clock"
        )
        replace_ports.assert_called_once_with(
            ports,
            vector=adapters.vector_searcher,
            keyword=adapters.keyword_searcher,
            history=adapters.search_history_store,
        )
        search_type.assert_called_once_with(
            "postgres-search-ports",
            stored_settings={"search_mode": "hybrid"},
        )


if __name__ == "__main__":
    unittest.main()
