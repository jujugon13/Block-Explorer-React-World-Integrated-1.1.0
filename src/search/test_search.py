from __future__ import annotations

import json
import time
import unittest
from datetime import UTC, datetime, timedelta
from threading import Event

from src.embedding import EmbeddingTransportError, QueryEmbedder
from src.guardrails import GuardrailService
from src.infra.http.testing import request_over_uvicorn
from src.platform import PlatformApp
from src.search import (
    InMemorySearchHistory,
    PipelineInterpreter,
    SearchHistoryRetentionJob,
    SearchPorts,
    SearchService,
)
from src.search.calls import GenerationRunner, llm_complete
from src.shared import (
    EmbeddingUnavailable,
    LogicalCircuitBreaker,
    Principal,
    PublicError,
    Request,
    SearchAnswerHistoryRecord,
    SearchCitationHistoryRecord,
    SearchHistoryBundle,
    SearchHistoryRecord,
    SearchHit,
    SearchUnavailable,
)


D1 = "00000000-0000-0000-0000-000000000001"
D2 = "00000000-0000-0000-0000-000000000002"
D3 = "00000000-0000-0000-0000-000000000003"
C1 = "10000000-0000-0000-0000-000000000001"
P1 = Principal("user-1", user_id=1)
P2 = Principal("user-2", user_id=2)

BASE = {
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


def hits(*documents, score=0.9):
    return [
        SearchHit(
            f"10000000-0000-0000-0000-{index:012d}",
            document,
            f"content-{index}",
            score - index / 100,
            {"rank": index},
        )
        for index, document in enumerate(documents, 1)
    ]


class FakeIndex:
    def __init__(self, ids=(D1, D2, D3)):
        self.ids = frozenset(ids)

    def indexed_document_ids(self):
        return self.ids


class FakePermissions:
    def __init__(self, readable=(D1, D2, D3), live=(D1, D2, D3)):
        self.readable = set(readable)
        self.live = set(live)
        self.live_calls = []

    def readable_document_ids(self, principal, candidate_ids):
        return frozenset(set(candidate_ids) & self.readable)

    def live_readable_document_ids(self, principal, candidate_ids):
        return frozenset(
            document_id
            for document_id in candidate_ids
            if self.can_read_document(principal, document_id)
        )

    def can_read_document(self, principal, document_id):
        self.live_calls.append((principal.subject, document_id))
        return document_id in self.live


class FakeEmbedder:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def embed_query(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return (1.0,)


class FakeVector:
    def __init__(self, found=None, error=None):
        self.found = list(found or hits(D1))
        self.error = error
        self.calls = []

    def search(self, vector, document_ids, limit):
        self.calls.append((tuple(vector), frozenset(document_ids), limit))
        if self.error:
            raise self.error
        return [item for item in self.found if item.document_id in document_ids][:limit]


class FakeKeyword:
    def __init__(self, found=None, fail_query=None):
        self.found = list(found or hits(D1, score=4.0))
        self.fail_query = fail_query
        self.calls = []

    def search(self, query, document_ids, limit, *, timeout_seconds):
        self.calls.append((query, frozenset(document_ids), limit, timeout_seconds))
        if self.fail_query and self.fail_query in query:
            raise ValueError("keyword failure")
        return [item for item in self.found if item.document_id in document_ids][:limit]


class FakeLLM:
    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        value = self.responses.get(request.task)
        if callable(value):
            return value(request)
        if isinstance(value, Exception):
            raise value
        if value is not None:
            return value
        if request.task in {"faithfulness", "hallucination"}:
            return "1.0"
        if request.task == "hyde":
            return "hypothetical"
        if request.task == "generation":
            return "generated answer"
        return ""


class FakeReranker:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def score(self, query, contents, *, model):
        self.calls.append((query, tuple(contents), model))
        if self.error:
            raise self.error
        return tuple(float(len(contents) - index) for index in range(len(contents)))


class MemoryCache:
    def __init__(self, down=False):
        self.data = {}
        self.down = down
        self.set_calls = []
        self.deleted = []

    def get(self, key):
        if self.down:
            raise ConnectionError
        return self.data.get(key)

    def set(self, key, value, ttl_seconds):
        if self.down:
            raise ConnectionError
        self.data[key] = value
        self.set_calls.append((key, ttl_seconds))

    def delete(self, key):
        if self.down:
            raise ConnectionError
        self.data.pop(key, None)
        self.deleted.append(key)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def build(*, settings=None, found=None, permissions=None, embedder=None, keyword=None,
          llm=None, reranker=None, cache=None, history=None, clock=None, now=None):
    config = dict(BASE)
    config.update(settings or {})
    vector = FakeVector(found)
    keyword = keyword or FakeKeyword(found)
    embedder = embedder or FakeEmbedder()
    permissions = permissions or FakePermissions()
    llm = llm or FakeLLM()
    history = history if history is not None else InMemorySearchHistory()
    ports = SearchPorts(
        FakeIndex(), permissions, embedder, vector, keyword, llm,
        cache=cache, history=history, reranker=reranker,
        guardrails=GuardrailService(sleep=lambda _: None),
    )
    service = SearchService(
        ports,
        config,
        now=now or (lambda: datetime(2026, 8, 27, tzinfo=UTC)),
        monotonic=clock or (lambda: 0.0),
        sleep=lambda _: None,
        jitter=lambda low, high: low,
    )
    return service, vector, keyword, embedder, llm, permissions


def search_over_uvicorn(service, *, principal=P1, debug=False, query="q"):
    app = PlatformApp(principal_resolver=lambda _request: principal)
    service.mount(app)
    path = "/api/search/debug" if debug else "/api/search"
    return request_over_uvicorn(
        app.handle,
        Request(
            "POST",
            path,
            {"Content-Type": "application/json"},
            json.dumps({"query": query}).encode(),
        ),
    )


def response_header(response, name):
    return dict((key.lower(), value) for key, value in response.headers)[name.lower()]


class SearchAcceptanceTests(unittest.TestCase):
    def test_AC_RS_01_block_terminates_after_input_trace(self):
        service, *_ = build(settings={"injection_detection_enabled": True})
        state = {
            "query": "무시 시스템 프롬프트 지시 규칙 우회 출력",
            "results": [], "answer": "", "ports": service.ports, "principal": P1,
            "llm_breaker": service.llm_breaker,
        }
        run = PipelineInterpreter(timer=lambda: 0.0).run(
            state, settings={**BASE, "injection_detection_enabled": True}
        )
        self.assertEqual(["guardrail_input"], [item["name"] for item in run["pipeline_trace"]])
        self.assertEqual("GUARDRAIL_VIOLATION", run["state"]["public_error"][0])
        response = search_over_uvicorn(service, query=state["query"])
        body = json.loads(response.body)
        self.assertEqual((400, "GUARDRAIL_VIOLATION"), (response.status, body["code"]))

    def test_AC_RS_01_request_extras_cannot_disable_guardrails_and_port_is_required(self):
        service, *_ = build(settings={"injection_detection_enabled": True})
        with self.assertRaises(PublicError) as blocked:
            service.execute(
                {
                    "query": "무시 시스템 프롬프트 지시 규칙 우회 출력",
                    "injection_detection_enabled": False,
                },
                P1,
            )
        self.assertEqual("GUARDRAIL_VIOLATION", blocked.exception.code)

        ports = SearchPorts(
            FakeIndex(), FakePermissions(), FakeEmbedder(), FakeVector(),
            FakeKeyword(), FakeLLM(), guardrails=None,
            history=InMemorySearchHistory(),
        )
        with self.assertRaisesRegex(ValueError, "guardrails port is required"):
            SearchService(ports)
        ports = SearchPorts(
            FakeIndex(), FakePermissions(), FakeEmbedder(), FakeVector(),
            FakeKeyword(), FakeLLM(), guardrails=GuardrailService(), history=None,
        )
        with self.assertRaisesRegex(ValueError, "history port is required"):
            SearchService(ports)

    def test_AC_RS_05_unknown_mode_uses_hybrid(self):
        service, vector, keyword, *_ = build()
        service.execute({"query": "q", "search_mode": "nonsense"}, P1, debug=True)
        self.assertEqual(1, len(vector.calls))
        self.assertEqual(1, len(keyword.calls))

    def test_AC_RS_06_hybrid_search_durations_are_identical(self):
        service, *_ = build(settings={"search_mode": "hybrid"})
        trace = service.execute({"query": "q"}, P1, debug=True).body["pipeline_trace"]
        durations = {item["name"]: item["duration_ms"] for item in trace}
        self.assertEqual(durations["vector_search"], durations["keyword_search"])

    def test_AC_RS_07_document_scope_noop_has_no_trace(self):
        service, *_ = build(
            settings={"document_scope_enabled": True, "document_scope_top_n": 3},
            found=hits(D1, D2, D3),
        )
        trace = service.execute({"query": "q"}, P1, debug=True).body["pipeline_trace"]
        self.assertNotIn("document_scope", [item["name"] for item in trace])

    def test_AC_RS_08_unreranked_fusion_fails_gate_threshold(self):
        service, *_ = build(settings={
            "search_mode": "hybrid", "retrieval_quality_gate_enabled": True,
            "soft_mode": False,
        })
        result = service.execute({"query": "q"}, P1, debug=True)
        gate = next(item for item in result.body["pipeline_trace"] if item["name"] == "retrieval_gate")
        self.assertFalse(gate["passed"])
        self.assertEqual(service._settings({})["not_found_message"], result.body["answer"])

    def test_AC_RS_20_hyde_failure_falls_back_to_original(self):
        llm = FakeLLM({"hyde": RuntimeError("down")})
        service, _, _, embedder, *_ = build(settings={"hyde_enabled": True}, llm=llm)
        trace = service.execute({"query": "original"}, P1, debug=True).body["pipeline_trace"]
        hyde = next(item for item in trace if item["name"] == "hyde")
        self.assertEqual(["original"], embedder.calls)
        self.assertEqual((False, {"fallback": "original_query"}), (hyde["passed"], hyde["detail"]))

    def test_AC_RS_21_reranker_failure_keeps_basic_results(self):
        service, *_ = build(
            settings={"reranking_enabled": True}, reranker=FakeReranker(RuntimeError())
        )
        result = service.execute({"query": "q"}, P1, debug=True)
        rerank = next(item for item in result.body["pipeline_trace"] if item["name"] == "reranking")
        self.assertEqual(D1, result.body["results"][0]["document_id"])
        self.assertEqual((False, {"fallback": "basic_results"}), (rerank["passed"], rerank["detail"]))

    def test_AC_RS_22_variant_failure_fails_whole_search(self):
        llm = FakeLLM({"multi_query": "good\nbad"})
        service, *_ = build(
            settings={"search_mode": "hybrid", "multi_query_enabled": True},
            keyword=FakeKeyword(fail_query="bad"), llm=llm,
        )
        with self.assertRaisesRegex(PublicError, "SEARCH_SERVICE_ERROR"):
            service.execute({"query": "original"}, P1)

    def test_AC_RS_23_sixth_search_opens_embedding_breaker_over_uvicorn(self):
        transport_calls = []

        def transport(request):
            transport_calls.append(request)
            raise EmbeddingTransportError(503)

        embedder = QueryEmbedder(
            transport,
            model="model",
            truncate=lambda text, _limit: text,
            dimension=1,
            sleep=lambda _seconds: None,
            jitter=lambda low, _high: low,
        )
        service, *_ = build(embedder=embedder)
        for _ in range(5):
            response = search_over_uvicorn(service)
            self.assertEqual(
                (503, "EMBEDDING_SERVICE_ERROR"),
                (response.status, json.loads(response.body)["code"]),
            )
        before = len(transport_calls)
        response = search_over_uvicorn(service)
        self.assertEqual(
            (503, "CIRCUIT_BREAKER_OPEN", before),
            (response.status, json.loads(response.body)["code"], len(transport_calls)),
        )

    def test_AC_RS_24_cache_outage_degrades_to_one_miss(self):
        service, *_ = build(settings={"cache_enabled": True}, cache=MemoryCache(down=True))
        response = search_over_uvicorn(service)
        self.assertEqual((200, "MISS"), (response.status, response_header(response, "X-Cache")))
        self.assertEqual(1, service.cache_misses)

    def test_AC_RS_25_second_identical_request_hits_without_trace(self):
        cache = MemoryCache()
        service, vector, *_ = build(settings={"cache_enabled": True}, cache=cache)
        search_over_uvicorn(service)
        second = search_over_uvicorn(service)
        self.assertEqual("HIT", response_header(second, "X-Cache"))
        self.assertNotIn("pipeline_trace", json.loads(second.body))
        self.assertEqual(1, len(vector.calls))

    def test_AC_RS_26_top_k_overrides_reranker_and_cache_key(self):
        many = hits(*([D1] * 10))
        cache, reranker = MemoryCache(), FakeReranker()
        service, *_ = build(
            settings={"cache_enabled": True, "reranking_enabled": True},
            found=many, cache=cache, reranker=reranker,
        )
        first = service.execute({"query": "q", "top_k": 7}, P1)
        second = service.execute({"query": "q", "top_k": 6}, P1)
        self.assertEqual((7, 6), (len(first.body["results"]), len(second.body["results"])))
        self.assertEqual(2, len({key for key, _ in cache.set_calls}))

    def test_AC_RS_26_generation_and_D7_settings_are_in_cache_key(self):
        cache = MemoryCache()
        service, _, _, _, llm, _ = build(
            settings={"cache_enabled": True}, cache=cache
        )
        first = service.execute({"query": "q", "generate_answer": False}, P1)
        second = service.execute({"query": "q", "generate_answer": True}, P1)
        self.assertEqual(("MISS", "MISS"), (first.headers[0][1], second.headers[0][1]))
        self.assertEqual(1, len([call for call in llm.calls if call.task == "generation"]))

        stored = dict(BASE, cache_enabled=True, generate_answer=True,
                      llm_provider="p1", system_prompt="s1")
        cache, llm = MemoryCache(), FakeLLM()
        ports = SearchPorts(
            FakeIndex(), FakePermissions(), FakeEmbedder(), FakeVector(), FakeKeyword(), llm,
            cache=cache, guardrails=GuardrailService(sleep=lambda _: None),
            history=InMemorySearchHistory(),
        )
        service = SearchService(ports, lambda: stored, monotonic=lambda: 0.0,
                                sleep=lambda _: None)
        before = service.execute({"query": "settings"}, P1)
        stored.update(llm_provider="p2", system_prompt="s2")
        after = service.execute({"query": "settings"}, P1)
        requests = [call for call in llm.calls if call.task == "generation"]
        self.assertEqual(("MISS", "MISS"), (before.headers[0][1], after.headers[0][1]))
        self.assertEqual([("p1", "s1"), ("p2", "s2")],
                         [(call.provider, call.system_prompt) for call in requests])

        pii_settings = dict(BASE, cache_enabled=True, pii_detection_enabled=False)
        cache = MemoryCache()
        sensitive = [SearchHit(C1, D1, "주민번호 880101-1234567", 0.9)]
        ports = SearchPorts(
            FakeIndex(), FakePermissions(), FakeEmbedder(), FakeVector(sensitive),
            FakeKeyword(sensitive), FakeLLM(), GuardrailService(sleep=lambda _: None),
            InMemorySearchHistory(), cache=cache,
        )
        service = SearchService(ports, lambda: pii_settings, monotonic=lambda: 0.0,
                                sleep=lambda _: None)
        raw = service.execute({"query": "pii"}, P1)
        pii_settings["pii_detection_enabled"] = True
        masked = service.execute({"query": "pii"}, P1)
        self.assertEqual(("MISS", "MISS"), (raw.headers[0][1], masked.headers[0][1]))
        self.assertIn("880101-1234567", raw.body["results"][0]["content"])
        self.assertNotIn("880101-1234567", masked.body["results"][0]["content"])

    def test_AC_RS_27_normal_response_omits_trace_and_applies_live_llm_settings(self):
        stored = dict(BASE, generate_answer=True, llm_provider="p1", llm_model="m1",
                      llm_temperature=0.1, system_prompt="s1")
        llm = FakeLLM()
        ports = SearchPorts(
            FakeIndex(), FakePermissions(), FakeEmbedder(), FakeVector(), FakeKeyword(), llm,
            guardrails=GuardrailService(sleep=lambda _: None),
            history=InMemorySearchHistory(),
        )
        service = SearchService(ports, lambda: stored, monotonic=lambda: 0.0, sleep=lambda _: None)
        first = service.execute({"query": "q1"}, P1)
        request = next(call for call in llm.calls if call.task == "generation")
        self.assertNotIn("pipeline_trace", first.body)
        self.assertEqual(("p1", "m1", 0.1, "s1"),
                         (request.provider, request.model, request.temperature, request.system_prompt))
        stored.update(llm_provider="p2", llm_model="m2", llm_temperature=0.2, system_prompt="s2")
        service.execute({"query": "q2"}, P1)
        request = [call for call in llm.calls if call.task == "generation"][-1]
        self.assertEqual(("p2", "m2", 0.2, "s2"),
                         (request.provider, request.model, request.temperature, request.system_prompt))

    def test_AC_RS_27_D8_history_excludes_vectors_and_purges_after_90_days(self):
        now = datetime(2026, 8, 27, tzinfo=UTC)
        history = InMemorySearchHistory()
        service, *_ = build(
            settings={"generate_answer": True}, history=history, now=lambda: now
        )
        service.execute({"query": "plain question"}, P1)
        self.assertEqual((1, 1, 1),
                         (len(history.searches), len(history.answers), len(history.citations)))
        record = history.searches[0]
        self.assertEqual("plain question", record.query)
        self.assertNotIn("vector", record.__dataclass_fields__)
        self.assertNotIn("prompt", history.answers[0].__dataclass_fields__)
        self.assertNotIn("content", history.citations[0].__dataclass_fields__)

        old = now - timedelta(days=91)
        exact = now - timedelta(days=90)
        for moment in (old, exact):
            history.record(
                SearchHistoryBundle(
                    SearchHistoryRecord("user", "q", moment, 1.0, 1, "SUCCESS", "hash"),
                    SearchAnswerHistoryRecord("user", moment, "answer", "SUCCESS", "hash"),
                    (SearchCitationHistoryRecord("user", moment, 1, "chunk", D1),),
                )
            )
        cutoff = SearchHistoryRetentionJob(history, clock=lambda: now).run()
        self.assertEqual(now - timedelta(days=90), cutoff)
        self.assertEqual(2, len(history.searches))
        self.assertEqual(2, len(history.answers))
        self.assertEqual(2, len(history.citations))
        self.assertNotIn(old, {row.requested_at for row in history.searches})
        self.assertIn(exact, {row.requested_at for row in history.searches})

    def test_AC_RS_27_AC_RS_28_mount_registers_normal_and_debug_entrypoints(self):
        service, *_ = build()
        app = PlatformApp(principal_resolver=lambda request: P1)
        service.mount(app)
        normal = request_over_uvicorn(app.handle,
            Request("POST", "/api/search", {"Content-Type": "application/json"}, b'{"query":"q"}')
        )
        debug = request_over_uvicorn(app.handle,
            Request("POST", "/api/search/debug", {"Content-Type": "application/json"}, b'{"query":"q"}')
        )
        self.assertEqual((200, 200), (normal.status, debug.status))
        self.assertNotIn("pipeline_trace", json.loads(normal.body))
        trace = json.loads(debug.body)["pipeline_trace"]
        self.assertTrue({item["name"] for item in trace} <= set(service.pipeline.trace_allowlist))

    def test_AC_RS_29_empty_prefilter_skips_both_searches(self):
        service, vector, keyword, *_ = build(permissions=FakePermissions(readable=()))
        result = service.execute({"query": "q"}, P1)
        self.assertEqual([], result.body["results"])
        self.assertEqual((0, 0), (len(vector.calls), len(keyword.calls)))

    def test_AC_RS_30_live_revocation_removes_candidate(self):
        service, *_ = build(permissions=FakePermissions(readable=(D1,), live=()))
        self.assertEqual([], service.execute({"query": "q"}, P1).body["results"])

    def test_AC_RS_31_requester_is_part_of_cache_key(self):
        cache = MemoryCache()
        service, *_ = build(settings={"cache_enabled": True}, cache=cache)
        one = search_over_uvicorn(service, principal=P1)
        two = search_over_uvicorn(service, principal=P2)
        self.assertEqual(
            ("MISS", "MISS"),
            (response_header(one, "X-Cache"), response_header(two, "X-Cache")),
        )
        self.assertEqual(2, len(cache.data))

    def test_AC_RS_33_revoked_cached_document_discards_entire_entry(self):
        cache, permissions = MemoryCache(), FakePermissions(readable=(D1,), live=(D1,))
        service, *_ = build(settings={"cache_enabled": True}, cache=cache, permissions=permissions)
        search_over_uvicorn(service)
        permissions.live.clear()
        second = search_over_uvicorn(service)
        self.assertEqual("MISS", response_header(second, "X-Cache"))
        self.assertEqual([], json.loads(second.body)["results"])
        self.assertEqual(1, len(cache.deleted))

    def test_AC_RS_34_generation_deadline_returns_results_and_logical_breaker(self):
        clock = FakeClock()
        llm = FakeLLM({"generation": lambda request: setattr(clock, "value", 26.0) or "late"})
        service, *_ = build(settings={"generate_answer": True}, llm=llm, clock=clock)
        response = search_over_uvicorn(service, debug=True)
        result = json.loads(response.body)
        generation = next(item for item in result["pipeline_trace"] if item["name"] == "generation")
        self.assertEqual(("", 1, False),
                         (result["answer"], len(result["results"]), generation["passed"]))
        self.assertEqual(200, response.status)

        class IgnoresTimeout:
            def complete(self, request):
                time.sleep(0.2)
                return "too late"

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            llm_complete(
                IgnoresTimeout(), task="generation", prompt="q", settings=BASE,
                deadline_at=time.monotonic() + 0.02, clock=time.monotonic,
                sleep=lambda _: None,
            )
        self.assertLess(time.monotonic() - started, 0.15)

        late_clock = FakeClock()
        late = FakeLLM({"generation": lambda request: setattr(late_clock, "value", 26.0) or "late"})
        late_breaker = LogicalCircuitBreaker(threshold=1, clock=lambda: 0.0)
        with self.assertRaises(TimeoutError):
            llm_complete(
                late, task="generation", prompt="q", settings=BASE,
                deadline_at=25.0, clock=late_clock, sleep=lambda _: None,
                breaker=late_breaker,
            )
        with self.assertRaises(SearchUnavailable):
            llm_complete(
                late, task="generation", prompt="q", settings=BASE,
                sleep=lambda _: None, breaker=late_breaker,
            )
        self.assertEqual(1, len(late.calls))

        failing = FakeLLM({"generation": RuntimeError("down")})
        breaker = LogicalCircuitBreaker(clock=lambda: 0.0)
        for _ in range(5):
            with self.assertRaises(SearchUnavailable):
                llm_complete(failing, task="generation", prompt="q", settings=BASE,
                             sleep=lambda _: None, breaker=breaker)
        with self.assertRaises(SearchUnavailable):
            llm_complete(failing, task="generation", prompt="q", settings=BASE,
                         sleep=lambda _: None, breaker=breaker)
        self.assertEqual(20, len(failing.calls))

    def test_AC_RS_34_timed_out_generation_cannot_leak_parallel_calls(self):
        runner = GenerationRunner()
        entered, release, finished = Event(), Event(), Event()

        class BlockingLLM:
            def __init__(self):
                self.calls = 0

            def complete(self, _request):
                self.calls += 1
                entered.set()
                release.wait(1)
                finished.set()
                return "released"

        class NextLLM:
            def __init__(self):
                self.calls = 0

            def complete(self, _request):
                self.calls += 1
                return "next"

        blocking, next_llm = BlockingLLM(), NextLLM()
        with self.assertRaises(TimeoutError):
            llm_complete(
                blocking,
                task="generation",
                prompt="q",
                settings=BASE,
                deadline_at=time.monotonic() + 0.03,
                generation_runner=runner,
            )
        self.assertTrue(entered.is_set())

        with self.assertRaises(TimeoutError):
            llm_complete(
                next_llm,
                task="generation",
                prompt="q",
                settings=BASE,
                deadline_at=time.monotonic() + 0.03,
                generation_runner=runner,
            )
        self.assertEqual((1, 0), (blocking.calls, next_llm.calls))

        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(
            "next",
            llm_complete(
                next_llm,
                task="generation",
                prompt="q",
                settings=BASE,
                deadline_at=time.monotonic() + 1,
                generation_runner=runner,
            ),
        )

    def test_AC_RS_35_deadline_response_is_never_cached(self):
        clock, cache = FakeClock(), MemoryCache()
        llm = FakeLLM({"generation": lambda request: setattr(clock, "value", clock.value + 26.0) or "late"})
        service, vector, *_ = build(
            settings={"generate_answer": True, "cache_enabled": True},
            llm=llm, cache=cache, clock=clock,
        )
        first = search_over_uvicorn(service)
        second = search_over_uvicorn(service)
        self.assertEqual(
            ("MISS", "MISS"),
            (response_header(first, "X-Cache"), response_header(second, "X-Cache")),
        )
        self.assertEqual((0, 2), (len(cache.set_calls), len(vector.calls)))

    def test_AC_RS_36_embedding_failure_uses_common_error_envelope(self):
        service, *_ = build(embedder=FakeEmbedder(EmbeddingUnavailable()))
        app = PlatformApp(principal_resolver=lambda request: P1)
        app.add_route("POST", "/api/search", service.handler())
        response = request_over_uvicorn(app.handle, Request("POST", "/api/search", {"Content-Type": "application/json"}, b'{"query":"q"}'))
        body = json.loads(response.body)
        self.assertEqual((503, False, "EMBEDDING_SERVICE_ERROR"),
                         (response.status, body["success"], body["code"]))

        service, *_ = build()
        service.ports.vector.error = RuntimeError("database xyz")
        app = PlatformApp(principal_resolver=lambda request: P1)
        app.add_route("POST", "/api/search", service.handler())
        response = request_over_uvicorn(app.handle, Request("POST", "/api/search", {"Content-Type": "application/json"}, b'{"query":"q"}'))
        body = json.loads(response.body)
        self.assertEqual((503, "SEARCH_SERVICE_ERROR"), (response.status, body["code"]))
        self.assertIn("database xyz", body["message"])

    def test_AC_SYS_007_search_reports_only_first_body_violation(self):
        service, *_ = build()
        app = PlatformApp(principal_resolver=lambda request: P1)
        app.add_route("POST", "/api/search", service.handler())
        response = request_over_uvicorn(app.handle,
            Request(
                "POST",
                "/api/search",
                {"Content-Type": "application/json"},
                b'{"query":7,"top_k":false}',
            )
        )

        body = json.loads(response.body)
        self.assertEqual((400, "COMMON-002"), (response.status, body["code"]))
        self.assertEqual("query: 필수 문자열이어야 합니다.", body["message"])
        self.assertNotIn("top_k", body["message"])


if __name__ == "__main__":
    unittest.main()
