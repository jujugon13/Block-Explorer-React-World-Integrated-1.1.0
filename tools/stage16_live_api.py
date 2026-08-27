#!/usr/bin/env python3
"""Run the real document/search routes for the authorized stage-16 outage probe."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.run_stage16_external import child_environment


os.environ.update(child_environment())


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
        return (0.0,) * 1536


class _LanguageModel:
    def complete(self, request):
        return ""


def build():
    from src.application_postgres import (
        build_postgres_domain_components,
        build_postgres_ledger_adapters,
        build_postgres_search_service,
    )
    from src.auth import AuthService, InMemoryCache, TokenManager
    from src.documents import register_document_routes
    from src.guardrails import GuardrailService
    from src.infra.http import create_fastapi_app
    from src.infra.s3 import build_s3_storage
    from src.platform import PlatformApp
    from src.search import SearchPorts
    from tools.stage16_e2e import ensure_user

    run_id = os.environ["STAGE16_API_RUN_ID"]
    secret = os.environ["STAGE16_JWT_SECRET"]
    adapters = build_postgres_ledger_adapters(apply_migrations=False)
    domain = build_postgres_domain_components(build_s3_storage(), adapters)
    principal = ensure_user(domain, run_id)
    now = datetime.now(UTC)
    tokens = TokenManager(secret)
    auth = AuthService(domain.users, InMemoryCache(lambda: datetime.now(UTC)), tokens)
    search = build_postgres_search_service(
        SearchPorts(
            domain.indexing,
            domain.permissions,
            _Embedder(),
            _NoResults(),
            _NoResults(),
            _LanguageModel(),
            GuardrailService(),
            _NoResults(),
        ),
        adapters,
        stored_settings=SEARCH_SETTINGS,
    )
    platform = PlatformApp(auth.resolve_request)
    register_document_routes(platform, domain.documents)
    search.mount(platform)
    token = tokens.issue(int(principal.user_id), principal.subject, now)
    return create_fastapi_app(platform.handle), "Bearer " + token


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--token", action="store_true")
    args = parser.parse_args()
    app, authorization = build()
    if args.token:
        print(authorization)
        return 0
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
