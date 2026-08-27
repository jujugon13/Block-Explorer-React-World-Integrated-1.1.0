from __future__ import annotations

import os
import time
import unittest
from threading import BoundedSemaphore, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from src.embedding import EmbeddingRequest, EmbeddingTransportError
from src.infra.ai import (
    AIConfigurationError,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    LLM_MODEL,
    RERANKER_MODEL,
    LocalCrossEncoderReranker,
    OpenAIEmbeddingTransport,
    OpenAILanguageModel,
    OpenAITokenTruncator,
    build_ai_adapters,
)
from src.search.calls import llm_complete
from src.shared import LanguageModelRequest, SearchUnavailable


class _Endpoint:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    def create(self, **arguments):
        self.calls.append(arguments)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Client:
    def __init__(self, *, embedding_result=None, response_result=None) -> None:
        self.embeddings = _Endpoint(embedding_result)
        self.responses = _Endpoint(response_result)
        self.timeouts = []

    def with_options(self, *, timeout):
        self.timeouts.append(timeout)
        return self


class _Encoding:
    @staticmethod
    def encode(text):
        return list(text)

    @staticmethod
    def decode(tokens):
        return "".join(tokens)


class _RemoteError(RuntimeError):
    status_code = 429
    response = SimpleNamespace(headers={"retry-after-ms": "2500"})


class AIAdapterTests(unittest.TestCase):
    def test_IT_AI_001_official_embedding_shape_dimensions_and_retry_after(self):
        response = SimpleNamespace(
            model=EMBEDDING_MODEL,
            data=(SimpleNamespace(index=0, embedding=[0.0] * EMBEDDING_DIMENSION),),
        )
        client = _Client(embedding_result=response)
        transport = OpenAIEmbeddingTransport(client, BoundedSemaphore(1))
        result = transport(EmbeddingRequest(EMBEDDING_MODEL, ("query",), 5.0, 5.0))

        self.assertEqual(EMBEDDING_DIMENSION, len(result.items[0].vector))
        self.assertEqual(
            {
                "model": EMBEDDING_MODEL,
                "input": ["query"],
                "dimensions": EMBEDDING_DIMENSION,
            },
            client.embeddings.calls[0],
        )
        self.assertEqual([10.0], client.timeouts)

        failed = OpenAIEmbeddingTransport(
            _Client(embedding_result=_RemoteError()),
            BoundedSemaphore(1),
        )
        with self.assertRaises(EmbeddingTransportError) as raised:
            failed(EmbeddingRequest(EMBEDDING_MODEL, ("query",), 5.0, 5.0))
        self.assertEqual((429, 2.5), (
            raised.exception.status_code,
            raised.exception.retry_after_seconds,
        ))

    def test_IT_AI_002_token_cutoff_and_responses_api_arguments(self):
        truncator = OpenAITokenTruncator(_Encoding())
        self.assertEqual("x" * 8191, truncator("x" * 8192, 8191))

        client = _Client(response_result=SimpleNamespace(output_text="answer"))
        llm = OpenAILanguageModel(client, BoundedSemaphore(1))
        answer = llm.complete(LanguageModelRequest(
            task="generation",
            prompt="question",
            model=LLM_MODEL,
            temperature=0.3,
            timeout_seconds=25.0,
            system_prompt="system",
            provider="openai",
        ))
        self.assertEqual("answer", answer)
        self.assertEqual(
            {
                "model": LLM_MODEL,
                "input": "question",
                "temperature": 0.3,
                "instructions": "system",
            },
            client.responses.calls[0],
        )

        failed_client = _Client(response_result=_RemoteError())
        waits = []
        with self.assertRaises(SearchUnavailable):
            llm_complete(
                OpenAILanguageModel(failed_client, BoundedSemaphore(1)),
                task="generation",
                prompt="question",
                settings={
                    "llm_provider": "openai",
                    "llm_model": LLM_MODEL,
                    "llm_temperature": 0.3,
                },
                sleep=waits.append,
                jitter=lambda low, high: low,
            )
        self.assertEqual([2.5, 2.5, 2.5], waits)
        self.assertEqual(4, len(failed_client.responses.calls))

    def test_IT_AI_003_reranker_is_lazy_f32_contract_batch_one_and_serial(self):
        state = {"loads": 0, "active": 0, "maximum": 0, "batches": []}
        lock = Lock()

        class Model:
            def predict(self, pairs, *, batch_size, show_progress_bar, convert_to_numpy):
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                    state["batches"].append(batch_size)
                time.sleep(0.02)
                with lock:
                    state["active"] -= 1
                return [float(len(content)) for _query, content in pairs]

        def load():
            state["loads"] += 1
            return Model()

        reranker = LocalCrossEncoderReranker(load)
        self.assertEqual(0, state["loads"])
        outputs = []
        threads = [
            Thread(
                target=lambda content=content: outputs.append(
                    reranker.score("q", (content,), model=RERANKER_MODEL)
                )
            )
            for content in ("a", "bb")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(1, state["loads"])
        self.assertEqual(1, state["maximum"])
        self.assertEqual([1, 1], state["batches"])
        self.assertCountEqual([(1.0,), (2.0,)], outputs)

    def test_IT_AI_004_missing_key_and_project_cache_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(AIConfigurationError, "OPENAI_API_KEY"):
                build_ai_adapters()
        project_cache = os.path.join(os.getcwd(), "model-cache")
        with patch.dict(os.environ, {"HF_HOME": project_cache}, clear=True):
            with self.assertRaisesRegex(AIConfigurationError, "outside"):
                LocalCrossEncoderReranker(lambda: object())

    def test_IT_AI_005_embedding_settings_reach_the_real_adapter_contract(self):
        settings = {
            "embedding.connect-timeout": 2.0,
            "embedding.response-timeout": 3.0,
            "embedding.document-batch-response-timeout": 17.0,
            "embedding.batch-size": 7,
            "embedding.character-budget": 7000,
            "embedding.token-budget": 1700,
            "embedding.circuit-enabled": False,
            "embedding.circuit-failure-threshold": 8,
            "embedding.circuit-open-seconds": 11.0,
        }
        with patch(
            "src.infra.ai.adapters.OpenAITokenTruncator",
            return_value=lambda text, _limit: text,
        ):
            adapters = build_ai_adapters(_Client(), settings=settings)

        self.assertIs(adapters.query_embedder.breaker, adapters.batch_embedder.breaker)
        self.assertEqual(
            (False, 8, 11.0),
            (
                adapters.query_embedder.breaker.enabled,
                adapters.query_embedder.breaker.failure_threshold,
                adapters.query_embedder.breaker.recovery_seconds,
            ),
        )
        self.assertEqual(
            (2.0, 3.0),
            (
                adapters.query_embedder._connect_timeout,
                adapters.query_embedder._response_timeout,
            ),
        )
        self.assertEqual(
            (7, 7000, 1700, 2.0, 17.0),
            (
                adapters.batch_embedder.config.max_items,
                adapters.batch_embedder.config.char_budget,
                adapters.batch_embedder.config.token_budget,
                adapters.batch_embedder.config.connect_timeout,
                adapters.batch_embedder.config.response_timeout,
            ),
        )


if __name__ == "__main__":
    unittest.main()
