from __future__ import annotations

import itertools
import threading
import unittest
from datetime import UTC, datetime, timedelta

from src.indexing import IndexingService
from src.indexing.store import InMemoryIndexingStore
from src.shared import DocumentVersionRegistration, PublicError, document_search_id


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)


class _WorkerFailureStore(InMemoryIndexingStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_worker_id: int | None = None
        self.failed = False

    def save_worker(self, worker) -> None:
        if worker.id == self.fail_worker_id and not self.failed:
            self.failed = True
            raise ConnectionError("worker persistence unavailable")
        super().save_worker(worker)


class _CandidateFailureStore(InMemoryIndexingStore):
    def __init__(self) -> None:
        super().__init__()
        self.candidate_reads = 0

    def expired_job_ids(self, cutoff, batch_size):
        del cutoff, batch_size
        self.candidate_reads += 1
        raise ConnectionError("candidate query unavailable")


class _StaleCandidateStore(InMemoryIndexingStore):
    def __init__(self) -> None:
        super().__init__()
        self.forced_job_id: int | None = None

    def expired_job_ids(self, cutoff, batch_size):
        if self.forced_job_id is not None:
            return (self.forced_job_id,)
        return super().expired_job_ids(cutoff, batch_size)


class IndexingAcceptanceTests(unittest.TestCase):
    def service(self, *, jitter: float = 0.0) -> IndexingService:
        counter = itertools.count(1)
        return IndexingService(
            clock=lambda: NOW,
            uuid_factory=lambda: f"token-{next(counter)}",
            retry_jitter=jitter,
            random_uniform=lambda low, high: (low + high) / 2,
        )

    def owned(
        self,
        *,
        version_status: str = "UPLOADED",
        document_status: str = "UPLOADED",
        retry_count: int = 0,
        max_retries: int = 3,
    ):
        service = self.service()
        document = service.add_document(status=document_status)
        version = service.add_version(document.id, 1, status=version_status)
        document.status = document_status
        job = service.create_job(
            version.id, retry_count=retry_count, max_retries=max_retries
        )
        model = service.add_model(dimension=3)
        worker = service.register_worker("worker-instance", now=NOW)
        claim = service.claim(worker.id, NOW)
        token = str(claim.data["claimToken"])
        attempt = service.start_attempt(job.id, worker.id, token, NOW + timedelta(seconds=1))
        return service, document, version, job, model, worker, token, int(attempt.data["attemptId"])

    @staticmethod
    def embed(service: IndexingService, version_id: int, count: int = 1) -> None:
        service.put_chunks(version_id, (f"chunk-{index}" for index in range(count)))
        service.put_vectors(version_id, ((float(index), 1.0, 2.0) for index in range(count)))

    def assert_code(self, expected: str, call) -> None:
        with self.assertRaises(PublicError) as caught:
            call()
        self.assertEqual(expected, caught.exception.code)

    def test_AC_IDX_001_empty_queue_is_204(self):
        service = self.service()
        worker = service.register_worker("one", now=NOW)
        self.assertEqual(204, service.claim(worker.id, NOW).status)

    def test_AC_IDX_002_unknown_worker_is_404_code(self):
        service = self.service()
        self.assert_code("WORKER-001", lambda: service.claim(999, NOW))

    def test_AC_IDX_003_exact_death_boundary_rejects_claim(self):
        service = self.service()
        worker = service.register_worker("one", now=NOW)
        self.assert_code(
            "WORKER-002",
            lambda: service.claim(worker.id, NOW + timedelta(seconds=30)),
        )

    def test_AC_IDX_004_higher_priority_claimed_first(self):
        service = self.service()
        document = service.add_document()
        low_version = service.add_version(document.id, 1)
        high_version = service.add_version(document.id, 2)
        low = service.create_job(low_version.id, priority=0, created_at=NOW)
        high = service.create_job(high_version.id, priority=1, created_at=NOW + timedelta(seconds=1))
        worker = service.register_worker("one", now=NOW)
        claimed = service.claim(worker.id, NOW + timedelta(seconds=2))
        self.assertEqual(high.id, claimed.data["jobId"])
        self.assertEqual("PENDING", low.status)

    def test_IT_RUNTIME_004_configured_job_defaults_apply_to_direct_and_document_registration(self):
        service = IndexingService(
            clock=lambda: NOW,
            default_job_priority=9,
            default_job_max_retries=7,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        direct = service.create_job(version.id)
        self.assertEqual((9, 7), (direct.priority, direct.max_retries))

        service.register_document_version(
            DocumentVersionRegistration(
                20, "UPLOADED", None, 21, None, 21, 1, "UPLOADED", 22, NOW
            )
        )
        registered = service.state.jobs[22]
        self.assertEqual((9, 7), (registered.priority, registered.max_retries))

    def test_AC_IDX_005_future_job_is_not_claimed(self):
        service = self.service()
        document = service.add_document()
        version = service.add_version(document.id, 1)
        service.create_job(version.id, next_run_at=NOW + timedelta(seconds=1))
        worker = service.register_worker("one", now=NOW)
        self.assertEqual(204, service.claim(worker.id, NOW).status)

    def test_AC_IDX_006_concurrent_claim_has_one_winner(self):
        service = self.service()
        document = service.add_document()
        version = service.add_version(document.id, 1)
        service.create_job(version.id)
        workers = (
            service.register_worker("one", now=NOW),
            service.register_worker("two", now=NOW),
        )
        barrier = threading.Barrier(2)
        statuses: list[int] = []

        def run(worker_id: int) -> None:
            barrier.wait()
            statuses.append(service.claim(worker_id, NOW).status)

        threads = [threading.Thread(target=run, args=(worker.id,)) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([200, 204], sorted(statuses))

    def test_AC_IDX_007_claim_returns_token_and_processing(self):
        service, _, _, job, _, worker, token, _ = self.owned()
        detail = service.detail(job.id)
        self.assertTrue(token)
        self.assertEqual("PROCESSING", detail["status"])
        self.assertEqual(worker.id, detail["workerId"])

    def test_AC_IDX_008_detail_omits_claim_token(self):
        service, _, _, job, _, _, _, _ = self.owned()
        self.assertNotIn("claimToken", service.detail(job.id))

    def test_AC_IDX_009_wrong_token_rejects_renew(self):
        service, _, _, job, _, worker, _, _ = self.owned()
        self.assert_code(
            "EMBEDDING-JOB-003",
            lambda: service.renew(job.id, worker.id, "wrong", NOW + timedelta(seconds=2)),
        )

    def test_AC_IDX_010_expired_lease_rejects_renew(self):
        service, _, _, job, _, worker, token, _ = self.owned()
        self.assert_code(
            "EMBEDDING-JOB-004",
            lambda: service.renew(job.id, worker.id, token, NOW + timedelta(minutes=5)),
        )

    def test_AC_IDX_011_renew_changes_only_expiry_without_event(self):
        service, _, _, job, _, worker, token, _ = self.owned()
        before = service.detail(job.id)
        events = service.events(job.id)
        service.heartbeat(worker.id, NOW + timedelta(minutes=1))
        service.renew(job.id, worker.id, token, NOW + timedelta(minutes=1))
        after = service.detail(job.id)
        self.assertGreater(after["leaseExpiresAt"], before["leaseExpiresAt"])
        self.assertEqual(before["lockedAt"], after["lockedAt"])
        self.assertEqual(events, service.events(job.id))

    def test_AC_IDX_012_reclaim_preserves_first_started_time(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        first = service.detail(job.id)["firstStartedAt"]
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "retry",
            now=NOW + timedelta(seconds=2),
        )
        run_at = service.detail(job.id)["nextRunAt"]
        service.heartbeat(worker.id, run_at)
        service.claim(worker.id, run_at)
        self.assertEqual(first, service.detail(job.id)["firstStartedAt"])

    def test_AC_IDX_020_attempt_replay_consumes_no_number(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        replay = service.start_attempt(job.id, worker.id, token, NOW + timedelta(seconds=2))
        self.assertEqual(200, replay.status)
        self.assertEqual(attempt_id, replay.data["attemptId"])
        self.assertEqual(1, len(service.attempts(job.id)))

    def test_AC_IDX_021_chunk_replay_skips_storage_creator(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        calls = 0

        def creator():
            nonlocal calls
            calls += 1
            return ("text",)

        first = service.save_chunks(
            job.id, attempt_id, worker.id, token, creator=creator, now=NOW + timedelta(seconds=2)
        )
        replay = service.save_chunks(
            job.id, attempt_id, worker.id, token, creator=creator, now=NOW + timedelta(seconds=3)
        )
        self.assertEqual((201, 200, 1), (first.status, replay.status, calls))
        self.assertEqual(first.data, replay.data)

    def test_AC_IDX_022_embedding_replay_skips_external_call(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        service.save_chunks(
            job.id, attempt_id, worker.id, token, chunks=("text",), now=NOW + timedelta(seconds=2)
        )
        calls = 0

        def embedder(chunks, model):
            nonlocal calls
            calls += 1
            return ((1.0, 2.0, 3.0),)

        first = service.save_embeddings(
            job.id, attempt_id, worker.id, token, embedder, NOW + timedelta(seconds=3)
        )
        replay = service.save_embeddings(
            job.id, attempt_id, worker.id, token, embedder, NOW + timedelta(seconds=4)
        )
        self.assertEqual((201, 200, 1), (first.status, replay.status, calls))
        self.assertEqual(first.data, replay.data)

    def test_AC_IDX_023_completion_replay_keeps_one_event(self):
        service, _, version, job, _, worker, token, attempt_id = self.owned()
        self.embed(service, version.id)
        first = service.complete(
            job.id, attempt_id, worker.id, token, NOW + timedelta(seconds=2)
        )
        replay = service.complete(
            job.id, attempt_id, worker.id, token, NOW + timedelta(seconds=3)
        )
        completed = [event for event in service.events(job.id) if event["eventType"] == "INDEXED"]
        self.assertEqual((200, 200, first.data), (first.status, replay.status, replay.data))
        self.assertEqual(1, len(completed))

    def test_AC_IDX_024_conflicting_failure_replay_is_rejected(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "bad",
            now=NOW + timedelta(seconds=2),
        )
        self.assert_code(
            "EMBEDDING-JOB-007",
            lambda: service.fail(
                job.id,
                attempt_id,
                worker.id,
                token,
                "STORAGE_OBJECT_MISSING",
                "bad",
                now=NOW + timedelta(seconds=3),
            ),
        )

    def test_AC_IDX_025_identical_failure_replays_first_result(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        first = service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "bad",
            now=NOW + timedelta(seconds=2),
        )
        replay = service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "bad",
            now=NOW + timedelta(seconds=20),
        )
        self.assertEqual(first.data, replay.data)

    def test_AC_IDX_030_vector_count_mismatch_blocks_completion(self):
        service, _, version, job, _, worker, token, attempt_id = self.owned()
        service.put_chunks(version.id, ("one", "two"))
        service.put_vectors(version.id, ((1.0, 2.0, 3.0),))
        self.assert_code(
            "DOCUMENT-INDEXING-003",
            lambda: service.complete(
                job.id, attempt_id, worker.id, token, NOW + timedelta(seconds=2)
            ),
        )

    def test_AC_IDX_031_two_live_jobs_block_completion(self):
        service, _, version, job, _, worker, token, attempt_id = self.owned()
        self.embed(service, version.id)
        service.create_job(version.id)
        self.assert_code(
            "DOCUMENT-INDEXING-003",
            lambda: service.complete(
                job.id, attempt_id, worker.id, token, NOW + timedelta(seconds=2)
            ),
        )

    def test_AC_IDX_032_newer_version_blocks_stale_completion(self):
        service, document, version, job, _, worker, token, attempt_id = self.owned()
        self.embed(service, version.id)
        service.add_version(document.id, 2)
        self.assert_code(
            "DOCUMENT-INDEXING-002",
            lambda: service.complete(
                job.id, attempt_id, worker.id, token, NOW + timedelta(seconds=2)
            ),
        )

    def test_AC_IDX_033_previous_vectors_become_stale(self):
        service = self.service()
        document = service.add_document()
        old = service.add_version(document.id, 1, status="INDEXED", indexed_at=NOW)
        model = service.add_model(dimension=3)
        service.put_chunks(old.id, ("old",))
        old_vectors = service.put_vectors(old.id, ((1.0, 2.0, 3.0),), model_id=model.id)
        old.status = "INDEXED"
        document.current_version_id = old.id
        document.status = "INDEXED"
        current = service.add_version(document.id, 2)
        job = service.create_job(current.id)
        worker = service.register_worker("one", now=NOW)
        claim = service.claim(worker.id, NOW)
        token = str(claim.data["claimToken"])
        attempt = service.start_attempt(job.id, worker.id, token, NOW + timedelta(seconds=1))
        self.embed(service, current.id)
        service.complete(
            job.id, int(attempt.data["attemptId"]), worker.id, token, NOW + timedelta(seconds=2)
        )
        self.assertEqual("STALE", old_vectors[0].status)

    def test_AC_IDX_034_completion_uses_one_common_timestamp(self):
        service, _, version, job, _, worker, token, attempt_id = self.owned()
        self.embed(service, version.id)
        service.complete(job.id, attempt_id, worker.id, token, NOW + timedelta(seconds=2))
        attempt = service.state.attempts[attempt_id]
        self.assertEqual(job.completed_at, attempt.ended_at)
        self.assertEqual(job.completed_at, version.indexed_at)

    def test_AC_IDX_035_retry_preserves_version_state_and_adds_retry_event(self):
        service, _, version, job, _, worker, token, attempt_id = self.owned(
            version_status="PARSING", document_status="INDEXING"
        )
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "STORAGE_UNAVAILABLE",
            "down",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(("PENDING", "PARSING"), (job.status, version.status))
        self.assertEqual("RETRY", service.events(job.id)[-1]["eventType"])

    def test_AC_IDX_036_nonretryable_failure_is_immediately_final(self):
        service, _, version, job, _, worker, token, attempt_id = self.owned()
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "bad",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(("FAILED", "FAILED", 0), (job.status, version.status, job.retry_count))

    def test_AC_IDX_037_failed_new_version_preserves_old_searchable_version(self):
        service = self.service()
        document = service.add_document()
        old = service.add_version(document.id, 1, status="INDEXED", indexed_at=NOW)
        document.current_version_id = old.id
        document.status = "INDEXED"
        current = service.add_version(document.id, 2, status="EMBEDDING")
        job = service.create_job(current.id)
        service.add_model(dimension=3)
        worker = service.register_worker("one", now=NOW)
        claim = service.claim(worker.id, NOW)
        token = str(claim.data["claimToken"])
        attempt = service.start_attempt(job.id, worker.id, token, NOW + timedelta(seconds=1))
        service.fail(
            job.id,
            int(attempt.data["attemptId"]),
            worker.id,
            token,
            "EMBEDDING_RESULT_INVALID",
            "bad",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(("INDEXED", old.id), (document.status, document.current_version_id))
        self.assertIn(document_search_id(document.id), service.indexed_document_ids())

    def test_AC_IDX_038_first_version_final_failure_marks_document_failed(self):
        service, document, version, job, _, worker, token, attempt_id = self.owned()
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "DOCUMENT_CONTENT_INVALID",
            "bad",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(("FAILED", "FAILED"), (document.status, version.status))

    def test_AC_IDX_039_retry_after_is_minimum_delay(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        failed_at = NOW + timedelta(seconds=2)
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "EMBEDDING_PROVIDER_OVERLOADED",
            "429",
            retry_after=30,
            now=failed_at,
        )
        self.assertGreaterEqual(job.next_run_at, failed_at + timedelta(seconds=30))

    def test_AC_IDX_040_zero_jitter_uses_exact_initial_delay(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        failed_at = NOW + timedelta(seconds=2)
        service.fail(
            job.id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "retry",
            now=failed_at,
        )
        self.assertEqual(failed_at + timedelta(seconds=10), job.next_run_at)

    def test_AC_IDX_041_ownership_loss_leaves_attempt_started(self):
        service, _, _, job, _, worker, token, attempt_id = self.owned()
        job.claim_token = "new-owner-token"
        result = service.report_failure_from_worker(
            job.id,
            attempt_id,
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "lost",
            now=NOW + timedelta(seconds=2),
        )
        self.assertIsNone(result)
        self.assertEqual("STARTED", service.state.attempts[attempt_id].status)
        self.assertFalse(any(event["eventType"] == "FAILED" for event in service.events(job.id)))

    def test_IT_STAGE16_dead_worker_failure_does_not_block_other_worker_or_job(self):
        store = _WorkerFailureStore()
        service = IndexingService(
            store,
            clock=lambda: NOW,
            lease_duration=timedelta(seconds=5),
            dead_threshold=timedelta(seconds=30),
            retry_initial=timedelta(seconds=1),
            retry_jitter=0,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id)
        first = service.register_worker("first", now=NOW)
        second = service.register_worker("second", now=NOW)
        service.claim(second.id, NOW)
        store.fail_worker_id = first.id

        summary = service.recover_expired(NOW + timedelta(seconds=31))

        self.assertEqual(1, summary["deadWorkers"])
        self.assertEqual(1, summary["workerFailures"])
        self.assertEqual(1, summary["recovered"])
        self.assertEqual("ACTIVE", store.get_worker(first.id).status)
        self.assertEqual("DEAD", store.get_worker(second.id).status)
        self.assertEqual("PENDING", store.get_job(job.id).status)

    def test_IT_STAGE16_candidate_query_failure_ends_only_the_tick(self):
        store = _CandidateFailureStore()
        service = IndexingService(store, clock=lambda: NOW)

        summary = service.recover_expired(NOW)

        self.assertEqual(1, store.candidate_reads)
        self.assertEqual(1, summary["candidateReadFailures"])
        self.assertEqual(0, summary["candidates"])

    def test_IT_STAGE16_stale_recovery_candidate_is_skipped_and_old_token_is_fenced(self):
        store = _StaleCandidateStore()
        old_tokens = itertools.count(1)
        service = IndexingService(
            store,
            clock=lambda: NOW,
            uuid_factory=lambda: f"old-token-{next(old_tokens)}",
            lease_duration=timedelta(seconds=5),
            dead_threshold=timedelta(seconds=30),
            retry_initial=timedelta(seconds=1),
            retry_jitter=0,
        )
        competing = IndexingService(
            store,
            clock=lambda: NOW,
            uuid_factory=lambda: "new-token",
            lease_duration=timedelta(seconds=5),
            dead_threshold=timedelta(seconds=30),
            retry_initial=timedelta(seconds=1),
            retry_jitter=0,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id)
        old_worker = service.register_worker("old-worker", now=NOW)
        old_claim = service.claim(old_worker.id, NOW)
        old_token = str(old_claim.data["claimToken"])
        old_attempt = service.start_attempt(
            job.id, old_worker.id, old_token, NOW + timedelta(seconds=1)
        )
        store.forced_job_id = job.id
        first = service.recover_expired(NOW + timedelta(seconds=31))
        second = competing.recover_expired(NOW + timedelta(seconds=31))

        self.assertEqual((1, 1), (first["recovered"], second["skipped"]))
        self.assertEqual(
            1,
            sum(
                event["eventType"] == "LEASE_EXPIRED"
                for event in service.events(job.id)
            ),
        )
        reclaim_at = NOW + timedelta(seconds=32)
        new_worker = competing.register_worker("new-worker", now=reclaim_at)
        new_claim = competing.claim(new_worker.id, reclaim_at)
        self.assertNotEqual(old_token, new_claim.data["claimToken"])
        self.assert_code(
            "EMBEDDING-JOB-003",
            lambda: service.renew(job.id, old_worker.id, old_token, reclaim_at),
        )
        self.assert_code(
            "EMBEDDING-JOB-003",
            lambda: service.complete(
                job.id,
                int(old_attempt.data["attemptId"]),
                old_worker.id,
                old_token,
                reclaim_at,
            ),
        )

    def test_AC_IDX_060_pending_job_cannot_be_manually_retried(self):
        service = self.service()
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id)
        self.assert_code("EMBEDDING-JOB-008", lambda: service.manual_retry(job.id, NOW))

    def test_AC_IDX_061_superseded_failed_version_cannot_be_retried(self):
        service = self.service()
        document = service.add_document(status="FAILED")
        old = service.add_version(document.id, 1, status="FAILED")
        job = service.create_job(old.id, status="FAILED")
        service.add_version(document.id, 2, status="UPLOADED")
        self.assert_code("EMBEDDING-JOB-009", lambda: service.manual_retry(job.id, NOW))

    def test_AC_IDX_062_manual_retry_with_chunks_resumes_chunked(self):
        service = self.service()
        document = service.add_document(status="FAILED")
        version = service.add_version(document.id, 1, status="FAILED")
        job = service.create_job(version.id, status="FAILED")
        service.put_chunks(version.id, ("saved",))
        version.status = "FAILED"
        service.manual_retry(job.id, NOW)
        self.assertEqual("CHUNKED", version.status)

    def test_AC_IDX_063_manual_retry_without_chunks_resumes_uploaded(self):
        service = self.service()
        document = service.add_document(status="FAILED")
        version = service.add_version(document.id, 1, status="FAILED")
        job = service.create_job(version.id, status="FAILED")
        service.manual_retry(job.id, NOW)
        self.assertEqual("UPLOADED", version.status)

    def test_AC_IDX_064_exhausted_manual_retry_gets_only_one_attempt(self):
        service = self.service()
        document = service.add_document(status="FAILED")
        version = service.add_version(document.id, 1, status="FAILED")
        job = service.create_job(version.id, status="FAILED", retry_count=3)
        worker = service.register_worker("one", now=NOW)
        service.manual_retry(job.id, NOW)
        claim = service.claim(worker.id, NOW)
        token = str(claim.data["claimToken"])
        attempt = service.start_attempt(job.id, worker.id, token, NOW + timedelta(seconds=1))
        service.fail(
            job.id,
            int(attempt.data["attemptId"]),
            worker.id,
            token,
            "WORKER_INTERNAL_ERROR",
            "again",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(("FAILED", 3), (job.status, job.retry_count))

    def test_AC_IDX_065_retry_all_reports_partial_success(self):
        service = self.service()
        for number in range(3):
            document = service.add_document(status="FAILED")
            version = service.add_version(document.id, 1, status="FAILED")
            service.create_job(version.id, status="FAILED")
            if number == 2:
                service.add_version(document.id, 2, status="UPLOADED")
        result = service.retry_all(NOW)
        self.assertEqual(200, result.status)
        self.assertEqual((2, 1, 0), (
            result.data["retriedCount"], result.data["skippedCount"], result.data["failedCount"]
        ))
        self.assertEqual("재처리 2건, 대상 제외 1건, 오류 0건입니다.", result.data["message"])

    def test_AC_IDX_066_retry_all_empty_is_normal_200(self):
        result = self.service().retry_all(NOW)
        self.assertEqual(200, result.status)
        self.assertEqual(
            (0, 0, 0, 0),
            tuple(result.data[key] for key in (
                "scannedCount", "retriedCount", "skippedCount", "failedCount"
            )),
        )

    def test_IT_AI_REGISTRY_001_empty_insert_exact_noop_and_conflicts_fail(self):
        expected = {
            "provider": "OPENAI",
            "model_name": "text-embedding-3-small",
            "model_version": "text-embedding-3-small",
            "dimension": 1536,
        }
        service = self.service()
        created = service.ensure_embedding_model(**expected)
        same = service.ensure_embedding_model(**expected)
        self.assertIs(created, same)
        self.assertEqual(1, len(service.state.models))

        service.add_model("other", 1536)
        with self.assertRaisesRegex(RuntimeError, "exactly one matching"):
            service.ensure_embedding_model(**expected)

        different = self.service()
        different.add_model("other", 1536)
        with self.assertRaisesRegex(RuntimeError, "exactly one matching"):
            different.ensure_embedding_model(**expected)
        self.assertEqual(1, len(different.state.models))

    def test_IT_STAGE16_expired_lease_rewinds_embedding_version_so_retry_can_run(self):
        counter = itertools.count(1)
        service = IndexingService(
            clock=lambda: NOW,
            uuid_factory=lambda: f"token-{next(counter)}",
            lease_duration=timedelta(seconds=5),
            dead_threshold=timedelta(seconds=300),
            retry_initial=timedelta(seconds=1),
            retry_jitter=0.0,
            random_uniform=lambda low, high: (low + high) / 2,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id)
        service.add_model(dimension=3)
        worker = service.register_worker("worker-instance", now=NOW)
        token = str(service.claim(worker.id, NOW).data["claimToken"])
        attempt_id = int(
            service.start_attempt(job.id, worker.id, token, NOW).data["attemptId"]
        )
        service.save_chunks(
            job.id, attempt_id, worker.id, token, chunks=("text",), now=NOW
        )

        def lost_primary(chunks, model):
            raise ConnectionError("primary lost mid-embedding")

        with self.assertRaises(ConnectionError):
            service.save_embeddings(
                job.id, attempt_id, worker.id, token, lost_primary, NOW
            )
        self.assertEqual("EMBEDDING", service.state.versions[version.id].status)

        summary = service.recover_expired(NOW + timedelta(seconds=31))

        self.assertEqual(1, summary["recovered"])
        self.assertEqual("PENDING", service.state.jobs[job.id].status)
        self.assertEqual("CHUNKED", service.state.versions[version.id].status)

        resumed = NOW + timedelta(seconds=60)
        retry_token = str(service.claim(worker.id, resumed).data["claimToken"])
        retry_attempt = int(
            service.start_attempt(job.id, worker.id, retry_token, resumed).data["attemptId"]
        )
        replay = service.save_chunks(
            job.id, retry_attempt, worker.id, retry_token, chunks=("text",), now=resumed
        )
        self.assertEqual(200, replay.status)
        service.save_embeddings(
            job.id,
            retry_attempt,
            worker.id,
            retry_token,
            lambda chunks, model: ((1.0, 2.0, 3.0),),
            resumed,
        )
        service.complete(
            job.id, retry_attempt, worker.id, retry_token, resumed + timedelta(seconds=1)
        )
        self.assertEqual("INDEXED", service.state.jobs[job.id].status)
        self.assertEqual("INDEXED", service.state.versions[version.id].status)
        self.assertEqual(1, len(service.state.vectors))

    def test_IT_STAGE16_retry_inheriting_complete_vectors_still_reaches_indexed(self):
        counter = itertools.count(1)
        service = IndexingService(
            clock=lambda: NOW,
            uuid_factory=lambda: f"token-{next(counter)}",
            lease_duration=timedelta(seconds=300),
            dead_threshold=timedelta(seconds=3600),
            retry_initial=timedelta(seconds=1),
            retry_jitter=0.0,
            random_uniform=lambda low, high: (low + high) / 2,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id)
        service.add_model(dimension=3)
        worker = service.register_worker("worker-instance", now=NOW)
        token = str(service.claim(worker.id, NOW).data["claimToken"])
        attempt_id = int(
            service.start_attempt(job.id, worker.id, token, NOW).data["attemptId"]
        )
        service.save_chunks(
            job.id, attempt_id, worker.id, token, chunks=("text",), now=NOW
        )
        service.save_embeddings(
            job.id, attempt_id, worker.id, token, lambda chunks, model: ((1.0, 2.0, 3.0),), NOW
        )
        # the worker dies after the vectors commit but before complete() runs
        self.assertEqual("STARTED", service.state.attempts[attempt_id].status)
        self.assertEqual(1, len(service.state.vectors))

        service.recover_expired(NOW + timedelta(seconds=305))
        self.assertEqual("PENDING", service.state.jobs[job.id].status)

        resumed = NOW + timedelta(seconds=315)
        retry_token = str(service.claim(worker.id, resumed).data["claimToken"])
        retry_attempt = int(
            service.start_attempt(job.id, worker.id, retry_token, resumed).data["attemptId"]
        )
        chunked = service.save_chunks(
            job.id, retry_attempt, worker.id, retry_token, chunks=("text",), now=resumed
        )
        embedded = service.save_embeddings(
            job.id, retry_attempt, worker.id, retry_token,
            lambda chunks, model: ((9.0, 9.0, 9.0),), resumed,
        )
        self.assertEqual((200, 200), (chunked.status, embedded.status))
        service.complete(
            job.id, retry_attempt, worker.id, retry_token, resumed + timedelta(seconds=1)
        )

        self.assertEqual("INDEXED", service.state.jobs[job.id].status)
        self.assertEqual("INDEXED", service.state.versions[version.id].status)
        self.assertEqual(1, len(service.state.vectors), "the replay must not duplicate vectors")
        self.assertEqual((1.0, 2.0, 3.0), tuple(service.state.vectors[0].values))

    def exhausted(self):
        """A job that burned every retry: version FAILED, its vectors staled."""
        counter = itertools.count(1)
        service = IndexingService(
            clock=lambda: NOW,
            uuid_factory=lambda: f"token-{next(counter)}",
            lease_duration=timedelta(seconds=300),
            dead_threshold=timedelta(seconds=3600),
            retry_initial=timedelta(seconds=1),
            retry_jitter=0.0,
            random_uniform=lambda low, high: (low + high) / 2,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id, retry_count=3, max_retries=3)
        service.add_model(dimension=3)
        worker = service.register_worker("worker-instance", now=NOW)
        token = str(service.claim(worker.id, NOW).data["claimToken"])
        attempt_id = int(service.start_attempt(job.id, worker.id, token, NOW).data["attemptId"])
        service.save_chunks(job.id, attempt_id, worker.id, token, chunks=("text",), now=NOW)
        service.save_embeddings(
            job.id, attempt_id, worker.id, token, lambda chunks, model: ((1.0, 2.0, 3.0),), NOW
        )
        service.fail(
            job.id, attempt_id, worker.id, token,
            "WORKER_INTERNAL_ERROR", "lost", now=NOW + timedelta(seconds=1),
        )
        return service, document, version, job, worker

    def test_IT_STAGE16_manual_retry_reactivates_staled_vectors_and_completes(self):
        service, _, version, job, worker = self.exhausted()
        self.assertEqual("FAILED", service.state.jobs[job.id].status)
        self.assertEqual(["STALE"], [row.status for row in service.state.vectors])

        service.manual_retry(job.id, NOW + timedelta(seconds=10))

        resumed = NOW + timedelta(seconds=20)
        token = str(service.claim(worker.id, resumed).data["claimToken"])
        attempt_id = int(
            service.start_attempt(job.id, worker.id, token, resumed).data["attemptId"]
        )
        service.save_chunks(job.id, attempt_id, worker.id, token, chunks=("text",), now=resumed)
        service.save_embeddings(
            job.id, attempt_id, worker.id, token,
            lambda chunks, model: ((1.0, 2.0, 3.0),), resumed,
        )
        service.complete(
            job.id, attempt_id, worker.id, token, resumed + timedelta(seconds=1)
        )

        self.assertEqual("INDEXED", service.state.jobs[job.id].status)
        self.assertEqual("INDEXED", service.state.versions[version.id].status)
        self.assertEqual(["ACTIVE"], [row.status for row in service.state.vectors])

    def test_IT_STAGE16_retry_never_revives_a_deleted_documents_vectors(self):
        service, document, _, job, worker = self.exhausted()
        deleted = service.state.documents[document.id]
        deleted.status = "DELETED"
        deleted.deleted_at = NOW + timedelta(seconds=5)
        service.state.jobs[job.id].status = "PENDING"
        service.state.jobs[job.id].retry_count = 0

        resumed = NOW + timedelta(seconds=20)
        token = str(service.claim(worker.id, resumed).data["claimToken"])
        attempt_id = int(
            service.start_attempt(job.id, worker.id, token, resumed).data["attemptId"]
        )
        service.save_embeddings(
            service.state.jobs[job.id].id, attempt_id, worker.id, token,
            lambda chunks, model: ((1.0, 2.0, 3.0),), resumed,
        )

        self.assertEqual(
            ["STALE"],
            [row.status for row in service.state.vectors],
            "a deleted document's vectors must stay out of the active set",
        )


if __name__ == "__main__":
    unittest.main()
