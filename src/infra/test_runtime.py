from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

try:
    import pypdf  # noqa: F401
except ModuleNotFoundError:
    pypdf = ModuleType("pypdf")
    pypdf.PdfReader = object
    sys.modules["pypdf"] = pypdf

from src.infra.runtime import _chunk_producer, build_application
from src.infra.settings import DEFAULT_SETTINGS
from src.shared import StorageLocation


def _settings(**overrides: object) -> dict[str, object]:
    return {**DEFAULT_SETTINGS, "jwt.secret": "test-secret", **overrides}


@dataclass
class _Version:
    file_object_id: int


@dataclass
class _File:
    location: StorageLocation
    document_type: str


class _Indexing:
    def detail(self, job_id: int) -> dict[str, object]:
        return {"jobId": job_id, "documentVersionId": 9}


class _DocumentStore:
    def version(self, version_id: int) -> _Version | None:
        return _Version(7) if version_id == 9 else None

    def file(self, file_id: int) -> _File | None:
        if file_id != 7:
            return None
        return _File(StorageLocation("local", "vectorshelf", "source.txt", 5), "TXT")


class _Storage:
    def get(self, location: StorageLocation) -> bytes:
        if location.key != "source.txt":
            raise AssertionError("unexpected storage location")
        return b"hello"


class RuntimeCompositionTests(unittest.TestCase):
    def test_IT_RUNTIME_001_chunk_producer_only_reads_and_parses_claimed_source(self):
        producer = _chunk_producer(
            _Indexing(), _DocumentStore(), _Storage(), _settings()
        )

        chunks = producer(4, 5, 6, "claim")

        self.assertEqual(1, len(chunks))
        self.assertEqual("hello", chunks[0].text)
        self.assertEqual(0, chunks[0].index)

    def test_IT_RUNTIME_002_real_composition_wires_cache_and_sync_role_flags(self):
        cache = object()
        postgres_config = object()
        cache_config = object()
        adapters = SimpleNamespace(
            vector_searcher=object(),
            keyword_searcher=object(),
            search_history_store=object(),
            document_store=object(),
        )
        domain = SimpleNamespace(
            users=SimpleNamespace(),
            indexing=SimpleNamespace(),
            permissions=SimpleNamespace(),
            documents=SimpleNamespace(),
            collections=SimpleNamespace(),
            sync=SimpleNamespace(),
            sync_dispatcher=object(),
        )
        ai = SimpleNamespace(
            query_embedder=object(),
            llm=object(),
            reranker=object(),
            embed_documents=object(),
        )
        search = SimpleNamespace()
        application = object()
        settings = _settings(
            **{
                "embedding.connect-timeout": 2.0,
                "indexing.job.max-retries": 8,
                "indexing.job.default-priority": -2,
                "sync.dispatcher.enabled": False,
                "sync.reconciliation.enabled": True,
            }
        )
        with (
            patch("src.infra.runtime.validate_settings") as validate,
            patch("src.infra.runtime.PostgresConfig.from_env", return_value=postgres_config) as postgres_from_env,
            patch("src.infra.runtime.RedisConfig.from_env", return_value=cache_config) as redis_from_env,
            patch("src.infra.runtime.validate_ai_configuration") as validate_ai,
            patch("src.infra.runtime._storage", return_value=object()),
            patch(
                "src.infra.runtime.build_postgres_ledger_adapters",
                return_value=adapters,
            ) as ledger_builder,
            patch("src.infra.runtime.build_postgres_domain_components", return_value=domain) as domain_builder,
            patch("src.infra.runtime.build_ai_adapters", return_value=ai) as ai_builder,
            patch("src.infra.runtime.build_redis_cache", return_value=cache) as cache_builder,
            patch("src.infra.runtime.build_postgres_search_service", return_value=search) as search_builder,
            patch("src.infra.runtime.build_postgres_mcp_service", return_value=object()),
            patch("src.infra.runtime.create_application", return_value=application) as compose,
        ):
            result = build_application(settings)

        self.assertIs(application, result)
        validate.assert_called_once_with(settings)
        postgres_from_env.assert_called_once_with()
        redis_from_env.assert_called_once_with()
        validate_ai.assert_called_once_with()
        ledger_builder.assert_called_once_with(postgres_config)
        cache_builder.assert_called_once_with(cache_config)
        ai_builder.assert_called_once_with(settings=settings)
        ports = search_builder.call_args.args[0]
        self.assertIs(cache, ports.cache)
        self.assertIs(ai.reranker, ports.reranker)
        self.assertEqual(
            False,
            domain_builder.call_args.kwargs["sync_options"]["dispatcher_enabled"],
        )
        self.assertEqual(
            True,
            domain_builder.call_args.kwargs["sync_options"]["reconciliation_enabled"],
        )
        self.assertEqual(
            (8, -2),
            (
                domain_builder.call_args.kwargs["indexing_options"]["default_job_max_retries"],
                domain_builder.call_args.kwargs["indexing_options"]["default_job_priority"],
            ),
        )
        components = compose.call_args.args[1]
        self.assertIs(cache, components.auth.cache)
        self.assertIs(domain.sync_dispatcher, components.sync_dispatcher)

    def test_IT_RUNTIME_003_no_sync_thread_is_composed_when_both_roles_are_disabled(self):
        cache = object()
        adapters = SimpleNamespace(
            vector_searcher=object(),
            keyword_searcher=object(),
            search_history_store=object(),
            document_store=object(),
        )
        domain = SimpleNamespace(
            users=SimpleNamespace(),
            indexing=SimpleNamespace(),
            permissions=SimpleNamespace(),
            documents=SimpleNamespace(),
            collections=SimpleNamespace(),
            sync=SimpleNamespace(),
            sync_dispatcher=object(),
        )
        ai = SimpleNamespace(
            query_embedder=object(),
            llm=object(),
            reranker=object(),
            embed_documents=object(),
        )
        with (
            patch("src.infra.runtime.validate_settings"),
            patch("src.infra.runtime.PostgresConfig.from_env", return_value=object()),
            patch("src.infra.runtime.RedisConfig.from_env", return_value=object()),
            patch("src.infra.runtime.validate_ai_configuration"),
            patch("src.infra.runtime._storage", return_value=object()),
            patch("src.infra.runtime.build_postgres_ledger_adapters", return_value=adapters),
            patch("src.infra.runtime.build_postgres_domain_components", return_value=domain),
            patch("src.infra.runtime.build_ai_adapters", return_value=ai),
            patch("src.infra.runtime.build_redis_cache", return_value=cache),
            patch("src.infra.runtime.build_postgres_search_service", return_value=SimpleNamespace()),
            patch("src.infra.runtime.build_postgres_mcp_service", return_value=object()),
            patch("src.infra.runtime.create_application", return_value=object()) as compose,
        ):
            build_application(_settings())

        self.assertIsNone(compose.call_args.args[1].sync_dispatcher)


if __name__ == "__main__":
    unittest.main()
