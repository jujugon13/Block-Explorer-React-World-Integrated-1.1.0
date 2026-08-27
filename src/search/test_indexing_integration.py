from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.guardrails import GuardrailService
from src.indexing import IndexingService, IndexVectorSearcher
from src.permissions import PermissionService
from src.search import InMemorySearchHistory, SearchPorts, SearchService
from src.shared import Principal


NOW = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
OWNER = Principal("owner@example.com", user_id=1)
READER = Principal("reader@example.com", user_id=2)
SETTINGS = {
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


class _QueryEmbedder:
    def embed_query(self, text: str):
        return (1.0, 0.0)


class _Keyword:
    def search(self, query, document_ids, limit, *, timeout_seconds):
        return ()


class _LanguageModel:
    def complete(self, request):
        raise AssertionError("answer generation is disabled")


class _CountingVector:
    def __init__(self, delegate, before_return=None) -> None:
        self.delegate = delegate
        self.before_return = before_return
        self.calls = 0

    def search(self, vector, document_ids, limit):
        self.calls += 1
        result = self.delegate.search(vector, document_ids, limit)
        if self.before_return is not None:
            self.before_return()
        return result


class IndexSearchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.indexing = IndexingService(
            clock=lambda: NOW,
            uuid_factory=lambda: "claim-token",
            retry_jitter=0.0,
        )
        self.indexing.add_model(dimension=2)
        self.documents = DocumentWorkspace(
            MemoryStorage(), indexing=self.indexing, clock=lambda: NOW
        )
        uploaded = self.documents.upload(
            OWNER,
            UploadFile(b"alpha", "alpha.txt", "text/plain"),
            title="Alpha",
            description=None,
            visibility="PRIVATE",
        )
        self.document_id = int(uploaded.data["documentId"])
        self.job_id = int(uploaded.data["embeddingJobId"])

        worker = self.indexing.register_worker("search-worker", now=NOW)
        claim = self.indexing.claim(worker.id, NOW)
        token = str(claim.data["claimToken"])
        attempt = self.indexing.start_attempt(
            self.job_id, worker.id, token, NOW + timedelta(seconds=1)
        )
        attempt_id = int(attempt.data["attemptId"])
        self.indexing.save_chunks(
            self.job_id,
            attempt_id,
            worker.id,
            token,
            chunks=("alpha",),
            now=NOW + timedelta(seconds=2),
        )
        self.indexing.save_embeddings(
            self.job_id,
            attempt_id,
            worker.id,
            token,
            lambda chunks, model: ((1.0, 0.0),),
            NOW + timedelta(seconds=3),
        )
        self.indexing.complete(
            self.job_id,
            attempt_id,
            worker.id,
            token,
            NOW + timedelta(seconds=4),
        )
        self.indexing.complete(
            self.job_id,
            attempt_id,
            worker.id,
            token,
            NOW + timedelta(seconds=5),
        )
        self.permissions = PermissionService(
            self.documents, clock=lambda: NOW
        )

    def service(self, vector) -> SearchService:
        return SearchService(
            SearchPorts(
                self.indexing,
                self.permissions,
                _QueryEmbedder(),
                vector,
                _Keyword(),
                _LanguageModel(),
                GuardrailService(sleep=lambda _: None),
                InMemorySearchHistory(),
            ),
            SETTINGS,
            now=lambda: NOW,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )

    def test_AC_IDX_023_AC_RS_27_completed_vectors_return_uuid_results_without_trace(self):
        vector = _CountingVector(IndexVectorSearcher(self.indexing))
        response = self.service(vector).execute({"query": "alpha"}, OWNER)
        result = response.body["results"][0]

        self.assertEqual((1, "alpha"), (vector.calls, result["content"]))
        self.assertNotIn("pipeline_trace", response.body)
        UUID(result["chunk_id"])
        UUID(result["document_id"])
        self.assertEqual(
            self.document_id,
            self.documents.document_access(result["document_id"]).resource_id,
        )
        self.assertEqual(
            1,
            sum(
                event.event_type == "INDEXED"
                for event in self.indexing.state.events
                if event.job_id == self.job_id
            ),
        )
        large = vector.delegate.search(
            (1e308, 1e308), self.indexing.indexed_document_ids(), 1
        )
        self.assertTrue(math.isfinite(large[0].score))

    def test_AC_RS_29_unreadable_indexed_uuid_skips_real_vector_search(self):
        vector = _CountingVector(IndexVectorSearcher(self.indexing))
        response = self.service(vector).execute({"query": "alpha"}, READER)

        self.assertEqual([], response.body["results"])
        self.assertEqual(0, vector.calls)

    def test_AC_RS_30_stale_permission_cache_is_removed_by_uuid_livecheck(self):
        permission = self.permissions.grant(
            OWNER,
            "DOCUMENT",
            self.document_id,
            "READ",
            target_type="USER",
            user_id=READER.user_id,
        )

        def revoke_ledger_only() -> None:
            self.permissions._permissions.pop(permission.permission_id)

        vector = _CountingVector(
            IndexVectorSearcher(self.indexing), before_return=revoke_ledger_only
        )
        response = self.service(vector).execute({"query": "alpha"}, READER)

        self.assertEqual(1, vector.calls)
        self.assertIn((READER.user_id, self.document_id), self.permissions._user_cache)
        self.assertEqual([], response.body["results"])


if __name__ == "__main__":
    unittest.main()
