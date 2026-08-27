from __future__ import annotations

import unittest
from dataclasses import dataclass

from src.embedding import (
    BatchEmbedder,
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
    EmbeddingItem,
    EmbeddingResponse,
    EmbeddingServiceError,
    EmbeddingTransportError,
    QueryEmbedder,
)


def _response(texts: tuple[str, ...], *, model: str = "model", dimension: int = 2):
    return EmbeddingResponse(
        model,
        tuple(
            EmbeddingItem(index, (float(len(text)),) * dimension)
            for index, text in enumerate(texts)
        ),
    )


class _Transport:
    def __init__(self, outcome=None) -> None:
        self.outcome = outcome
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        outcome = self.outcome
        if callable(outcome):
            outcome = outcome(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else _response(request.texts)


@dataclass(frozen=True)
class _Chunk:
    text: str
    token_estimate: int


class EmbeddingAcceptanceTests(unittest.TestCase):
    def test_AC_RS_23_query_and_document_embedding_contract(self):
        with self.subTest("circuit open duration is strictly positive"):
            with self.assertRaises(ValueError):
                CircuitBreaker(1, 0)

        with self.subTest("five logical failures open query breaker"):
            transport = _Transport(EmbeddingTransportError(503))
            waits = []
            limits = []
            embedder = QueryEmbedder(
                transport,
                model="model",
                truncate=lambda text, limit: limits.append(limit) or text,
                dimension=2,
                sleep=waits.append,
                jitter=lambda low, high: low,
            )
            for _ in range(5):
                with self.assertRaises(EmbeddingServiceError) as raised:
                    embedder.embed_query("query")
                self.assertEqual((503, "EMBEDDING_SERVICE_ERROR"), (raised.exception.status, raised.exception.code))
            self.assertEqual(20, len(transport.requests))
            with self.assertRaises(CircuitBreakerOpen) as raised:
                embedder.embed_query("query")
            self.assertEqual((503, "CIRCUIT_BREAKER_OPEN"), (raised.exception.status, raised.exception.code))
            self.assertEqual(20, len(transport.requests))
            self.assertEqual([0.5, 1.0, 2.0], waits[:3])
            self.assertEqual([8191] * 6, limits)

        with self.subTest("non-retryable status has one attempt"):
            transport = _Transport(EmbeddingTransportError(400))
            embedder = QueryEmbedder(
                transport,
                model="model",
                truncate=lambda text, limit: text,
                sleep=lambda seconds: self.fail("must not sleep"),
            )
            with self.assertRaises(EmbeddingServiceError):
                embedder.embed_query("query")
            self.assertEqual(1, len(transport.requests))

        with self.subTest("retry-after is a lower bound for retry delay"):
            outcomes = [EmbeddingTransportError(429, 7.0), _response(("query",))]
            transport = _Transport(lambda _request: outcomes.pop(0))
            waits = []
            QueryEmbedder(
                transport,
                model="model",
                truncate=lambda text, limit: text,
                sleep=waits.append,
                jitter=lambda low, high: low,
            ).embed_query("query")
            self.assertEqual([7.0], waits)

        with self.subTest("only one half-open probe"):
            now = [0.0]
            breaker = CircuitBreaker(1, 30.0, clock=lambda: now[0])
            breaker.record_failure(breaker.before_call())
            self.assertEqual(CircuitState.OPEN, breaker.state)
            now[0] = 30.0
            probe = breaker.before_call()
            self.assertEqual(CircuitState.HALF_OPEN, breaker.state)
            with self.assertRaises(CircuitBreakerOpen):
                breaker.before_call()
            breaker.record_success(probe)
            self.assertEqual(CircuitState.CLOSED, breaker.state)

        with self.subTest("batch boundaries preserve equality order and oversized singleton"):
            transport = _Transport()
            embedder = BatchEmbedder(transport, model="model", dimension=2)
            chunks = (
                _Chunk("a" * 2000, 450),
                _Chunk("b" * 2000, 450),
                _Chunk("c" * 4001, 901),
                _Chunk("d", 1),
            )
            vectors = embedder.embed(chunks)
            self.assertEqual([2, 1, 1], [len(request.texts) for request in transport.requests])
            self.assertEqual(tuple(float(len(chunk.text)) for chunk in chunks), tuple(v[0] for v in vectors))
            self.assertTrue(all(request.response_timeout == 30.0 for request in transport.requests))

        with self.subTest("document transport retries and opens after five logical failures"):
            transport = _Transport(EmbeddingTransportError(503))
            limits = []
            embedder = BatchEmbedder(
                transport,
                model="model",
                dimension=2,
                truncate=lambda text, limit: limits.append(limit) or text,
                sleep=lambda _seconds: None,
            )
            for _ in range(5):
                with self.assertRaises(EmbeddingServiceError):
                    embedder.embed((_Chunk("x", 1),))
            self.assertEqual(20, len(transport.requests))
            with self.assertRaises(CircuitBreakerOpen):
                embedder.embed((_Chunk("x", 1),))
            self.assertEqual(20, len(transport.requests))
            self.assertEqual([8191] * 6, limits)

        malformed = (
            EmbeddingResponse("", (EmbeddingItem(0, (1.0, 2.0)),)),
            EmbeddingResponse("model", ()),
            EmbeddingResponse("model", (EmbeddingItem(1, (1.0, 2.0)),)),
            EmbeddingResponse("model", (EmbeddingItem(0, (1.0,)),)),
            EmbeddingResponse("model", (EmbeddingItem(0, (1.0, float("nan"))),)),
        )
        for response in malformed:
            with self.subTest("malformed batch response", response=response):
                with self.assertRaises(EmbeddingServiceError):
                    BatchEmbedder(_Transport(response), model="model", dimension=2).embed(
                        (_Chunk("x", 1),)
                    )

        with self.subTest("later batch failure exposes no partial result"):
            calls = [0]

            def fail_second(request):
                calls[0] += 1
                if calls[0] >= 2:
                    return EmbeddingTransportError(503)
                return _response(request.texts)

            with self.assertRaises(EmbeddingServiceError):
                BatchEmbedder(
                    _Transport(fail_second),
                    model="model",
                    dimension=2,
                    sleep=lambda _seconds: None,
                ).embed(
                    (_Chunk("a" * 4000, 1), _Chunk("b", 1))
                )
            self.assertEqual(5, calls[0])
