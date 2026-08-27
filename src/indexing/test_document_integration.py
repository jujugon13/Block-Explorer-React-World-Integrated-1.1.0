from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO

from src.documents import DocumentWorkspace, UploadFile, register_document_routes
from src.documents.testing import MemoryStorage
from src.indexing import IndexingService
from src.platform import PlatformApp
from src.shared import Principal
from src.sync import SyncService


NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
OWNER = Principal("owner@example.com", user_id=1, display_name="Owner")


def _http_call(app, method: str, path: str, *, body: bytes = b"", content_type=None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])

    raw = b"".join(app(environ, start_response))
    return captured["status"], json.loads(raw) if raw else None


def _version_body(data: bytes) -> tuple[bytes, str]:
    boundary = "index-integration"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="file.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class _MutateThenFailOutbox(SyncService):
    def __init__(self) -> None:
        super().__init__(clock=lambda: NOW)
        self.fail_version = False

    def publish_document_version_created(self, *args, **kwargs):
        event = super().publish_document_version_created(*args, **kwargs)
        if self.fail_version:
            raise RuntimeError("outbox failed after insert")
        return event


class DocumentIndexIntegrationTests(unittest.TestCase):
    def stack(self, outbox: SyncService | None = None):
        storage = MemoryStorage()
        indexing = IndexingService(
            clock=lambda: NOW,
            uuid_factory=lambda: "claim-token",
            retry_jitter=0.0,
        )
        indexing.add_model(dimension=3)
        documents = DocumentWorkspace(
            storage,
            indexing=indexing,
            sync_outbox=outbox,
            clock=lambda: NOW,
        )
        return storage, documents, indexing

    @staticmethod
    def upload(documents: DocumentWorkspace, data: bytes = b"alpha"):
        return documents.upload(
            OWNER,
            UploadFile(data, "file.txt", "text/plain"),
            title="Title",
            description=None,
            visibility="PRIVATE",
        )

    @staticmethod
    def own(indexing: IndexingService, result, offset: int = 0):
        worker = indexing.register_worker(
            f"worker-{offset}", now=NOW + timedelta(seconds=offset)
        )
        claim = indexing.claim(worker.id, NOW + timedelta(seconds=offset))
        token = str(claim.data["claimToken"])
        attempt = indexing.start_attempt(
            int(result.data["embeddingJobId"]),
            worker.id,
            token,
            NOW + timedelta(seconds=offset + 1),
        )
        return worker, claim, token, int(attempt.data["attemptId"])

    @staticmethod
    def index_success(indexing, result, worker, token, attempt_id, offset: int = 0):
        job_id = int(result.data["embeddingJobId"])
        indexing.save_chunks(
            job_id,
            attempt_id,
            worker.id,
            token,
            chunks=("alpha",),
            now=NOW + timedelta(seconds=offset + 2),
        )
        indexing.save_embeddings(
            job_id,
            attempt_id,
            worker.id,
            token,
            lambda chunks, model: ((1.0, 2.0, 3.0),),
            NOW + timedelta(seconds=offset + 3),
        )
        return indexing.complete(
            job_id,
            attempt_id,
            worker.id,
            token,
            NOW + timedelta(seconds=offset + 4),
        )

    def test_AC_DOC_014_AC_IDX_001_upload_registers_the_claimed_job(self):
        storage, documents, indexing = self.stack()
        result = self.upload(documents)
        job_id = int(result.data["embeddingJobId"])
        version_id = int(result.data["documentVersionId"])
        document_id = int(result.data["documentId"])

        detail = indexing.detail(job_id)
        snapshot = documents.snapshot_for_chunking(version_id)
        worker, claim, _, _ = self.own(indexing, result)
        app = PlatformApp(lambda request: OWNER, lambda: NOW)
        register_document_routes(app, documents)
        status_code, payload = _http_call(
            app, "GET", f"/api/documents/{document_id}/status"
        )
        version_body, content_type = _version_body(b"beta")
        version_status, version_error = _http_call(
            app,
            "POST",
            f"/api/documents/{document_id}/versions",
            body=version_body,
            content_type=content_type,
        )

        self.assertEqual(200, claim.status)
        self.assertEqual(job_id, claim.data["jobId"])
        self.assertEqual(version_id, claim.data["documentVersionId"])
        self.assertEqual(document_id, detail["documentId"])
        self.assertEqual(version_id, detail["documentVersionId"])
        self.assertEqual("PENDING", result.data["jobStatus"])
        self.assertEqual((document_id, version_id), (snapshot.document_id, snapshot.version_id))
        self.assertEqual("TXT", snapshot.document_type)
        self.assertIn(snapshot.file_location.key, storage.objects)
        self.assertEqual(worker.id, indexing.detail(job_id)["workerId"])
        self.assertEqual(("PROCESSING", "UPLOADED", "INDEXING"), (
            documents.state.jobs[job_id].status,
            documents.state.versions[version_id].status,
            documents.state.documents[document_id].status,
        ))
        self.assertEqual((200, "INDEXING"), (
            status_code,
            payload["data"]["documentStatus"],
        ))
        self.assertEqual(
            {"versionNo": 1, "status": "UPLOADED", "jobStatus": "PROCESSING"},
            payload["data"]["processingVersion"],
        )
        self.assertEqual((409, "DOCUMENT-VERSION-002"), (
            version_status,
            version_error["code"],
        ))

    def test_AC_DOC_034_AC_IDX_021_AC_IDX_023_chunks_and_completion_share_one_ledger(self):
        _, documents, indexing = self.stack()
        result = self.upload(documents)
        worker, _, token, attempt_id = self.own(indexing, result)
        job_id = int(result.data["embeddingJobId"])
        version_id = int(result.data["documentVersionId"])
        document_id = int(result.data["documentId"])
        calls = 0

        def creator():
            nonlocal calls
            calls += 1
            self.assertEqual("PARSING", indexing.state.versions[version_id].status)
            self.assertEqual("PARSING", documents.state.versions[version_id].status)
            self.assertEqual("PROCESSING", documents.state.jobs[job_id].status)
            return ("alpha",)

        first = indexing.save_chunks(
            job_id,
            attempt_id,
            worker.id,
            token,
            creator=creator,
            now=NOW + timedelta(seconds=2),
        )
        replay = indexing.save_chunks(
            job_id,
            attempt_id,
            worker.id,
            token,
            creator=creator,
            now=NOW + timedelta(seconds=3),
        )
        document_chunks = documents.chunks_for_embedding(version_id)
        self.assertEqual((201, 200, 1), (first.status, replay.status, calls))
        self.assertEqual(indexing.state.chunks[version_id], document_chunks)
        self.assertEqual("CHUNKED", documents.state.versions[version_id].status)

        indexing.save_embeddings(
            job_id,
            attempt_id,
            worker.id,
            token,
            lambda chunks, model: ((1.0, 2.0, 3.0),),
            NOW + timedelta(seconds=4),
        )
        self.assertEqual(
            ("EMBEDDING", "EMBEDDING"),
            (
                indexing.state.versions[version_id].status,
                documents.state.versions[version_id].status,
            ),
        )
        completed = indexing.complete(
            job_id,
            attempt_id,
            worker.id,
            token,
            NOW + timedelta(seconds=5),
        )
        replayed = indexing.complete(
            job_id,
            attempt_id,
            worker.id,
            token,
            NOW + timedelta(seconds=6),
        )

        document = documents.state.documents[document_id]
        version = documents.state.versions[version_id]
        self.assertEqual(completed.data, replayed.data)
        self.assertEqual(("INDEXED", "INDEXED", "INDEXED"), (
            documents.state.jobs[job_id].status,
            version.status,
            document.status,
        ))
        self.assertEqual(version_id, document.current_version_id)
        self.assertEqual(indexing.detail(job_id)["completedAt"], version.indexed_at)
        self.assertEqual(
            1,
            sum(event["eventType"] == "INDEXED" for event in indexing.events(job_id)),
        )

    def test_AC_DOC_025_AC_IDX_025_AC_IDX_037_failure_results_are_mirrored(self):
        _, documents, indexing = self.stack()
        result = self.upload(documents)
        worker, _, token, attempt_id = self.own(indexing, result)
        job_id = int(result.data["embeddingJobId"])
        version_id = int(result.data["documentVersionId"])
        document_id = int(result.data["documentId"])

        first = indexing.fail(
            job_id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "retry",
            now=NOW + timedelta(seconds=2),
        )
        event_count = len(indexing.events(job_id))
        replay = indexing.fail(
            job_id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "retry",
            now=NOW + timedelta(seconds=20),
        )
        self.assertEqual(first.data, replay.data)
        self.assertEqual(event_count, len(indexing.events(job_id)))
        self.assertEqual("PENDING", documents.state.jobs[job_id].status)
        self.assertEqual(indexing.state.versions[version_id].status, documents.state.versions[version_id].status)
        self.assertEqual(indexing.state.documents[document_id].status, documents.state.documents[document_id].status)

        _, failed_documents, failed_indexing = self.stack()
        failed_result = self.upload(failed_documents)
        failed_worker, _, failed_token, failed_attempt = self.own(
            failed_indexing, failed_result
        )
        failed_indexing.fail(
            int(failed_result.data["embeddingJobId"]),
            failed_attempt,
            failed_worker.id,
            failed_token,
            "DOCUMENT_CONTENT_INVALID",
            "invalid",
            now=NOW + timedelta(seconds=2),
        )
        failed_document = failed_documents.state.documents[failed_result.data["documentId"]]
        failed_version = failed_documents.state.versions[failed_result.data["documentVersionId"]]
        self.assertEqual(("FAILED", "FAILED", None), (
            failed_document.status,
            failed_version.status,
            failed_document.current_version_id,
        ))

    def test_AC_IDX_025_failure_replay_survives_new_owner_and_lease(self):
        _, documents, indexing = self.stack()
        result = self.upload(documents)
        worker, _, token, attempt_id = self.own(indexing, result)
        job_id = int(result.data["embeddingJobId"])
        first = indexing.fail(
            job_id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "retry",
            now=NOW + timedelta(seconds=2),
        )

        replacement = indexing.register_worker(
            "replacement-worker", now=NOW + timedelta(seconds=12)
        )
        claimed = indexing.claim(replacement.id, NOW + timedelta(seconds=12))
        document_before = deepcopy(documents.state)
        events_before = indexing.events(job_id)
        replay = indexing.fail(
            job_id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "retry",
            now=NOW + timedelta(seconds=13),
        )

        detail = indexing.detail(job_id)
        self.assertEqual(first.data, replay.data)
        self.assertEqual("PENDING", replay.data["status"])
        self.assertEqual(("PROCESSING", replacement.id), (
            detail["status"],
            detail["workerId"],
        ))
        self.assertEqual(claimed.data["leaseExpiresAt"], detail["leaseExpiresAt"])
        self.assertEqual(events_before, indexing.events(job_id))
        self.assertEqual(document_before, documents.state)

    def test_AC_DOC_027_AC_IDX_037_new_version_failure_keeps_current_version(self):
        _, documents, indexing = self.stack()
        first = self.upload(documents)
        first_worker, _, first_token, first_attempt = self.own(indexing, first)
        self.index_success(indexing, first, first_worker, first_token, first_attempt)
        current_id = int(first.data["documentVersionId"])

        second = documents.add_version(
            OWNER,
            int(first.data["documentId"]),
            UploadFile(b"beta", "file.txt", "text/plain"),
        )
        worker, _, token, attempt_id = self.own(indexing, second, offset=5)
        indexing.fail(
            int(second.data["embeddingJobId"]),
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "invalid",
            now=NOW + timedelta(seconds=7),
        )

        document = documents.state.documents[first.data["documentId"]]
        self.assertEqual("INDEXED", document.status)
        self.assertEqual(current_id, document.current_version_id)
        self.assertEqual("FAILED", documents.state.versions[second.data["documentVersionId"]].status)
        self.assertEqual("FAILED", documents.state.jobs[second.data["embeddingJobId"]].status)

    def test_AC_IDX_035_AC_IDX_038_AC_IDX_063_retry_and_recovery_are_mirrored(self):
        _, documents, indexing = self.stack()
        result = self.upload(documents)
        worker, _, token, attempt_id = self.own(indexing, result)
        job_id = int(result.data["embeddingJobId"])
        version_id = int(result.data["documentVersionId"])
        document_id = int(result.data["documentId"])
        indexing.fail(
            job_id,
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "invalid",
            now=NOW + timedelta(seconds=2),
        )
        indexing.manual_retry(job_id, NOW + timedelta(seconds=3))
        self.assertEqual(("PENDING", "UPLOADED", "UPLOADED"), (
            documents.state.jobs[job_id].status,
            documents.state.versions[version_id].status,
            documents.state.documents[document_id].status,
        ))
        self.assertEqual(
            (
                indexing.state.jobs[job_id].status,
                indexing.state.versions[version_id].status,
                indexing.state.documents[document_id].status,
            ),
            ("PENDING", "UPLOADED", "UPLOADED"),
        )

        _, recovered_documents, recovered_indexing = self.stack()
        recovered_result = self.upload(recovered_documents)
        recovered_worker, _, _, _ = self.own(recovered_indexing, recovered_result)
        summary = recovered_indexing.recover_expired(
            NOW + timedelta(minutes=5), batch_size=10
        )
        recovered_job = int(recovered_result.data["embeddingJobId"])
        recovered_version = int(recovered_result.data["documentVersionId"])
        recovered_document = int(recovered_result.data["documentId"])
        self.assertEqual(1, summary["recovered"])
        self.assertEqual("DEAD", recovered_indexing.effective_worker_status(
            recovered_worker.id, NOW + timedelta(minutes=5)
        ))
        self.assertEqual(
            (
                recovered_indexing.state.jobs[recovered_job].status,
                recovered_indexing.state.versions[recovered_version].status,
                recovered_indexing.state.documents[recovered_document].status,
            ),
            (
                recovered_documents.state.jobs[recovered_job].status,
                recovered_documents.state.versions[recovered_version].status,
                recovered_documents.state.documents[recovered_document].status,
            ),
        )
        self.assertEqual("PENDING", recovered_documents.state.jobs[recovered_job].status)

        _, final_documents, final_indexing = self.stack()
        final_result = self.upload(final_documents)
        final_job = int(final_result.data["embeddingJobId"])
        final_indexing.state.jobs[final_job].max_retries = 0
        self.own(final_indexing, final_result)
        final_indexing.recover_expired(NOW + timedelta(minutes=5), batch_size=10)
        final_version = int(final_result.data["documentVersionId"])
        final_document = int(final_result.data["documentId"])
        self.assertEqual(("FAILED", "FAILED", "FAILED"), (
            final_documents.state.jobs[final_job].status,
            final_documents.state.versions[final_version].status,
            final_documents.state.documents[final_document].status,
        ))

    def test_AC_DOC_014_AC_SYNC_001_creation_failures_roll_back_both_ledgers(self):
        outbox = _MutateThenFailOutbox()
        storage, documents, indexing = self.stack(outbox)
        index_before = deepcopy(indexing.state)
        index_ids_before = dict(indexing._next)
        document_ids_before = dict(documents._next_ids)
        outbox.fail_version = True

        with self.assertRaises(RuntimeError):
            self.upload(documents)

        self.assertEqual(index_before, indexing.state)
        self.assertEqual(index_ids_before, indexing._next)
        self.assertEqual({}, documents.state.documents)
        self.assertEqual(document_ids_before, documents._next_ids)
        self.assertEqual((), outbox.events())
        self.assertEqual({}, storage.objects)

        outbox.fail_version = False
        first = self.upload(documents)
        worker, _, token, attempt_id = self.own(indexing, first)
        self.index_success(indexing, first, worker, token, attempt_id)
        document_before = deepcopy(documents.state)
        index_before = deepcopy(indexing.state)
        storage_before = dict(storage.objects)
        outbox_before = outbox.events()
        document_ids_before = dict(documents._next_ids)
        index_ids_before = dict(indexing._next)
        expected_version_id = documents._next_ids["version"]
        expected_job_id = documents._next_ids["job"]
        outbox.fail_version = True

        with self.assertRaises(RuntimeError):
            documents.add_version(
                OWNER,
                int(first.data["documentId"]),
                UploadFile(b"beta", "file.txt", "text/plain"),
            )

        self.assertEqual(document_before, documents.state)
        self.assertEqual(index_before, indexing.state)
        self.assertEqual(document_ids_before, documents._next_ids)
        self.assertEqual(index_ids_before, indexing._next)
        self.assertEqual(storage_before, storage.objects)
        self.assertEqual(outbox_before, outbox.events())

        outbox.fail_version = False
        retried = documents.add_version(
            OWNER,
            int(first.data["documentId"]),
            UploadFile(b"beta", "file.txt", "text/plain"),
        )
        self.assertEqual(expected_version_id, retried.data["documentVersionId"])
        self.assertEqual(expected_job_id, retried.data["embeddingJobId"])
        claimed = indexing.claim(worker.id, NOW + timedelta(seconds=5))
        self.assertEqual((200, expected_job_id), (claimed.status, claimed.data["jobId"]))

    def test_AC_IDX_021_AC_IDX_034_AC_IDX_037_document_commit_failures_roll_back_index(self):
        _, claim_documents, claim_indexing = self.stack()
        claim_result = self.upload(claim_documents)
        claim_worker = claim_indexing.register_worker("claim-worker", now=NOW)
        claim_before = deepcopy(claim_indexing.state)
        claim_ids_before = dict(claim_indexing._next)
        original_progress = claim_documents.commit_index_progress
        claim_documents.commit_index_progress = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("document progress failed")
        )
        with self.assertRaises(RuntimeError):
            claim_indexing.claim(claim_worker.id, NOW)
        self.assertEqual(claim_before, claim_indexing.state)
        self.assertEqual(claim_ids_before, claim_indexing._next)
        self.assertEqual("PENDING", claim_documents.state.jobs[claim_result.data["embeddingJobId"]].status)
        claim_documents.commit_index_progress = original_progress
        claimed = claim_indexing.claim(claim_worker.id, NOW)
        self.assertEqual(200, claimed.status)

        _, documents, indexing = self.stack()
        result = self.upload(documents)
        worker, _, token, attempt_id = self.own(indexing, result)
        job_id = int(result.data["embeddingJobId"])
        version_id = int(result.data["documentVersionId"])

        original_save = documents.save_chunks
        documents.save_chunks = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("document chunk commit failed")
        )
        with self.assertRaises(RuntimeError):
            indexing.save_chunks(
                job_id,
                attempt_id,
                worker.id,
                token,
                chunks=("alpha",),
                now=NOW + timedelta(seconds=2),
            )
        self.assertEqual("PARSING", indexing.state.versions[version_id].status)
        self.assertNotIn(version_id, indexing.state.chunks)
        self.assertNotIn(version_id, documents.state.chunks_by_version)
        documents.save_chunks = original_save
        indexing.save_chunks(
            job_id,
            attempt_id,
            worker.id,
            token,
            chunks=("alpha",),
            now=NOW + timedelta(seconds=3),
        )
        embedding_before = deepcopy(indexing.state)
        embedding_ids_before = dict(indexing._next)
        original_progress = documents.commit_index_progress
        documents.commit_index_progress = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("document embedding progress failed")
        )
        with self.assertRaises(RuntimeError):
            indexing.save_embeddings(
                job_id,
                attempt_id,
                worker.id,
                token,
                lambda chunks, model: ((1.0, 2.0, 3.0),),
                NOW + timedelta(seconds=4),
            )
        self.assertEqual(embedding_before, indexing.state)
        self.assertEqual(embedding_ids_before, indexing._next)
        self.assertEqual("CHUNKED", documents.state.versions[version_id].status)
        documents.commit_index_progress = original_progress
        indexing.save_embeddings(
            job_id,
            attempt_id,
            worker.id,
            token,
            lambda chunks, model: ((1.0, 2.0, 3.0),),
            NOW + timedelta(seconds=4),
        )

        index_before = deepcopy(indexing.state)
        index_ids_before = dict(indexing._next)
        document_before = deepcopy(documents.state)
        original_complete = documents.commit_index_result
        documents.commit_index_result = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("document completion failed")
        )
        with self.assertRaises(RuntimeError):
            indexing.complete(
                job_id,
                attempt_id,
                worker.id,
                token,
                NOW + timedelta(seconds=5),
            )
        self.assertEqual(index_before, indexing.state)
        self.assertEqual(index_ids_before, indexing._next)
        self.assertEqual(document_before, documents.state)
        documents.commit_index_result = original_complete
        indexing.complete(
            job_id,
            attempt_id,
            worker.id,
            token,
            NOW + timedelta(seconds=5),
        )

        _, failed_documents, failed_indexing = self.stack()
        failed_result = self.upload(failed_documents)
        failed_worker, _, failed_token, failed_attempt = self.own(
            failed_indexing, failed_result
        )
        failed_before = deepcopy(failed_indexing.state)
        failed_ids_before = dict(failed_indexing._next)
        original_failure = failed_documents.commit_index_failure
        failed_documents.commit_index_failure = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("document failure commit failed")
        )
        with self.assertRaises(RuntimeError):
            failed_indexing.fail(
                int(failed_result.data["embeddingJobId"]),
                failed_attempt,
                failed_worker.id,
                failed_token,
                "DOCUMENT_CONTENT_INVALID",
                "invalid",
                now=NOW + timedelta(seconds=2),
            )
        self.assertEqual(failed_before, failed_indexing.state)
        self.assertEqual(failed_ids_before, failed_indexing._next)
        failed_documents.commit_index_failure = original_failure
        failed_indexing.fail(
            int(failed_result.data["embeddingJobId"]),
            failed_attempt,
            failed_worker.id,
            failed_token,
            "DOCUMENT_CONTENT_INVALID",
            "invalid",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual("FAILED", failed_documents.state.documents[failed_result.data["documentId"]].status)


if __name__ == "__main__":
    unittest.main()
