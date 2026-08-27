"""Official OpenAI SDK and local CrossEncoder adapters for stage 13."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any

from src.embedding import (
    BatchConfig,
    BatchEmbedder,
    CircuitBreaker,
    EmbeddingItem,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingTransportError,
    QueryEmbedder,
)
from src.shared import ChunkRecord, LanguageModelRequest


EMBEDDING_PROVIDER = "OPENAI"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_MODEL_VERSION = "text-embedding-3-small"
EMBEDDING_DIMENSION = 1536
LLM_MODEL = "gpt-4.1-mini"
RERANKER_MODEL = "dragonkue/bge-reranker-v2-m3-ko"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AIConfigurationError(RuntimeError):
    """Fail startup without exposing a missing or invalid secret value."""


class OpenAIServiceError(RuntimeError):
    """Sanitized OpenAI error retaining only retry policy fields."""

    def __init__(
        self,
        status_code: int | None,
        retry_after_seconds: float | None,
    ) -> None:
        super().__init__("OpenAI service error")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _inside_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(_PROJECT_ROOT)
    except ValueError:
        return False
    return True


def _huggingface_cache() -> Path:
    configured = {
        name: Path(value).expanduser()
        for name in ("HF_HOME", "HF_HUB_CACHE")
        if (value := os.environ.get(name, "").strip())
    }
    for name, path in configured.items():
        if _inside_project(path):
            raise AIConfigurationError(f"{name} must be outside the project directory")
    if "HF_HUB_CACHE" in configured:
        return configured["HF_HUB_CACHE"].resolve()
    if "HF_HOME" in configured:
        return (configured["HF_HOME"] / "hub").resolve()
    return (Path.home() / ".cache" / "huggingface" / "hub").resolve()


def _retry_after(error: Exception) -> float | None:
    headers = getattr(getattr(error, "response", None), "headers", None)
    if not isinstance(headers, Mapping) and not hasattr(headers, "get"):
        return None
    milliseconds = headers.get("retry-after-ms")
    if milliseconds is not None:
        try:
            seconds = float(milliseconds) / 1000.0
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (TypeError, ValueError):
            pass
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return max(0.0, moment.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _status(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def validate_ai_configuration(environment: Mapping[str, str] | None = None) -> None:
    source = os.environ if environment is None else environment
    key = source.get("OPENAI_API_KEY", "")
    if not key.strip():
        raise AIConfigurationError("OPENAI_API_KEY is required")


def _positive_number(
    settings: Mapping[str, object], key: str, default: float
) -> float:
    value = settings.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise AIConfigurationError(f"{key} must be positive")
    return float(value)


def _positive_integer(
    settings: Mapping[str, object], key: str, default: int
) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AIConfigurationError(f"{key} must be a positive integer")
    return value


def _boolean(settings: Mapping[str, object], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if not isinstance(value, bool):
        raise AIConfigurationError(f"{key} must be boolean")
    return value


def _official_client() -> Any:
    validate_ai_configuration()
    key = os.environ.get("OPENAI_API_KEY", "")
    try:
        from openai import OpenAI
    except ImportError as error:
        raise AIConfigurationError("the official OpenAI SDK is required") from error
    return OpenAI(api_key=key, max_retries=0)


class OpenAITokenTruncator:
    """Truncate with the tokenizer for the startup-snapshotted embedding model."""

    def __init__(self, encoding: Any | None = None) -> None:
        if encoding is None:
            try:
                import tiktoken
            except ImportError as error:
                raise AIConfigurationError("tiktoken is required") from error
            encoding = tiktoken.encoding_for_model(EMBEDDING_MODEL)
        self._encoding = encoding

    def __call__(self, text: str, max_tokens: int) -> str:
        tokens = self._encoding.encode(text)
        return text if len(tokens) <= max_tokens else self._encoding.decode(tokens[:max_tokens])


class OpenAIEmbeddingTransport:
    """Use the official SDK with SDK retries disabled and fixed dimensions."""

    def __init__(self, client: Any, slot: BoundedSemaphore) -> None:
        self._client = client
        self._slot = slot

    def __call__(self, request: EmbeddingRequest) -> EmbeddingResponse:
        if request.model != EMBEDDING_MODEL:
            raise AIConfigurationError("embedding model differs from the startup model")
        timeout = request.connect_timeout + request.response_timeout
        if not self._slot.acquire(timeout=timeout):
            raise EmbeddingTransportError()
        try:
            response = self._client.with_options(timeout=timeout).embeddings.create(
                model=EMBEDDING_MODEL,
                input=list(request.texts),
                dimensions=EMBEDDING_DIMENSION,
            )
        except Exception as error:
            raise EmbeddingTransportError(_status(error), _retry_after(error)) from error
        finally:
            self._slot.release()
        return EmbeddingResponse(
            str(response.model),
            tuple(EmbeddingItem(int(item.index), item.embedding) for item in response.data),
        )


class OpenAILanguageModel:
    """Generate text through the official Responses API."""

    def __init__(self, client: Any, slot: BoundedSemaphore) -> None:
        self._client = client
        self._slot = slot

    def complete(self, request: LanguageModelRequest) -> str:
        if request.provider.casefold() != "openai" or request.model != LLM_MODEL:
            raise AIConfigurationError("LLM provider or model differs from startup")
        if not self._slot.acquire(timeout=request.timeout_seconds):
            raise TimeoutError("OpenAI concurrency slot timeout")
        arguments: dict[str, object] = {
            "model": LLM_MODEL,
            "input": request.prompt,
            "temperature": request.temperature,
        }
        if request.system_prompt is not None:
            arguments["instructions"] = request.system_prompt
        try:
            response = self._client.with_options(
                timeout=request.timeout_seconds
            ).responses.create(**arguments)
            output = response.output_text
            if not isinstance(output, str):
                raise TypeError("OpenAI response output is not text")
            return output
        except Exception as error:
            if isinstance(error, TimeoutError):
                raise
            raise OpenAIServiceError(_status(error), _retry_after(error)) from error
        finally:
            self._slot.release()


class LocalCrossEncoderReranker:
    """Lazy F32 CPU CrossEncoder with one in-process inference slot."""

    def __init__(self, model_loader: Any | None = None) -> None:
        self.cache_directory = _huggingface_cache()
        self._model_loader = model_loader or self._load_cross_encoder
        self._model: Any | None = None
        self._load_lock = Lock()
        self._inference = BoundedSemaphore(1)

    def _load_cross_encoder(self) -> Any:
        try:
            import torch
            from sentence_transformers import CrossEncoder
        except ImportError as error:
            raise AIConfigurationError("sentence-transformers and torch are required") from error
        return CrossEncoder(
            RERANKER_MODEL,
            device="cpu",
            cache_folder=str(self.cache_directory),
            model_kwargs={"torch_dtype": torch.float32},
        )

    def _loaded_model(self) -> Any:
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._model = self._model_loader()
        return self._model

    def score(
        self,
        query: str,
        contents: Sequence[str],
        *,
        model: str,
    ) -> tuple[float, ...]:
        if model != RERANKER_MODEL:
            raise AIConfigurationError("reranker model differs from startup")
        if not contents:
            return ()
        with self._inference:
            raw = self._loaded_model().predict(
                [(query, content) for content in contents],
                batch_size=1,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        scores = tuple(float(value) for value in raw)
        if len(scores) != len(contents) or any(not math.isfinite(value) for value in scores):
            raise RuntimeError("invalid reranker response")
        return scores


@dataclass(frozen=True, slots=True)
class AIAdapters:
    query_embedder: QueryEmbedder
    batch_embedder: BatchEmbedder
    llm: OpenAILanguageModel
    reranker: LocalCrossEncoderReranker

    def embed_documents(self, chunks: tuple[ChunkRecord, ...], model: Any):
        if (
            model.name != EMBEDDING_MODEL
            or model.dimension != EMBEDDING_DIMENSION
            or model.provider != EMBEDDING_PROVIDER
            or model.model_version != EMBEDDING_MODEL_VERSION
        ):
            raise AIConfigurationError("active embedding model differs from startup")
        return self.batch_embedder.embed(chunks)


def build_ai_adapters(
    client: Any | None = None,
    *,
    settings: Mapping[str, object] | None = None,
) -> AIAdapters:
    """Snapshot the fixed stage-13 configuration and fail before remote I/O."""

    values = settings or {}
    selected_client = client if client is not None else _official_client()
    slot = BoundedSemaphore(1)
    transport = OpenAIEmbeddingTransport(selected_client, slot)
    truncator = OpenAITokenTruncator()
    breaker = CircuitBreaker(
        _positive_integer(values, "embedding.circuit-failure-threshold", 5),
        _positive_number(values, "embedding.circuit-open-seconds", 30.0),
        enabled=_boolean(values, "embedding.circuit-enabled", True),
    )
    connect_timeout = _positive_number(values, "embedding.connect-timeout", 5.0)
    response_timeout = _positive_number(values, "embedding.response-timeout", 5.0)
    batch_config = BatchConfig(
        max_items=_positive_integer(values, "embedding.batch-size", 4),
        char_budget=_positive_integer(values, "embedding.character-budget", 4000),
        token_budget=_positive_integer(values, "embedding.token-budget", 900),
        connect_timeout=connect_timeout,
        response_timeout=_positive_number(
            values, "embedding.document-batch-response-timeout", 30.0
        ),
    )
    return AIAdapters(
        QueryEmbedder(
            transport,
            model=EMBEDDING_MODEL,
            truncate=truncator,
            dimension=EMBEDDING_DIMENSION,
            breaker=breaker,
            connect_timeout=connect_timeout,
            response_timeout=response_timeout,
        ),
        BatchEmbedder(
            transport,
            model=EMBEDDING_MODEL,
            dimension=EMBEDDING_DIMENSION,
            config=batch_config,
            breaker=breaker,
            truncate=truncator,
        ),
        OpenAILanguageModel(selected_client, slot),
        LocalCrossEncoderReranker(),
    )
