"""Injected embedding transports, batching, validation, and circuit breaking."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from threading import RLock
from typing import Protocol, TypeVar

from src.shared.search import EmbeddingCircuitOpen, EmbeddingUnavailable


_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})


class EmbeddingServiceError(EmbeddingUnavailable):
    """The embedding provider failed or returned an invalid response."""

    code = "EMBEDDING_SERVICE_ERROR"
    status = 503

    def __init__(self) -> None:
        super().__init__(self.code)


class CircuitBreakerOpen(EmbeddingCircuitOpen):
    """The provider is temporarily blocked without a transport call."""

    code = "CIRCUIT_BREAKER_OPEN"
    status = 503

    def __init__(self) -> None:
        super().__init__(self.code)


class EmbeddingTransportError(RuntimeError):
    """Adapter failure with an optional remote HTTP status."""

    def __init__(
        self,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            str(status_code) if status_code is not None else "embedding transport error"
        )
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    model: str
    texts: tuple[str, ...]
    connect_timeout: float
    response_timeout: float


@dataclass(frozen=True, slots=True)
class EmbeddingItem:
    index: int
    vector: Sequence[float]


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    model: str
    items: Sequence[EmbeddingItem]


class EmbeddingTransport(Protocol):
    def __call__(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


class TokenTruncator(Protocol):
    def __call__(self, text: str, max_tokens: int) -> str: ...


class EmbeddingChunk(Protocol):
    text: str
    token_estimate: int


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class _Permit:
    generation: int
    probe: bool


class CircuitBreaker:
    """Process-local breaker with one concurrent half-open probe."""

    def __init__(
        self,
        failure_threshold: int,
        recovery_seconds: float = 30.0,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_seconds <= 0:
            raise ValueError("invalid circuit breaker configuration")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.enabled = enabled
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._generation = 0
        self._lock = RLock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._failures

    def before_call(self) -> _Permit | None:
        if not self.enabled:
            return None
        with self._lock:
            if self._state is CircuitState.OPEN:
                if self._clock() - self._opened_at < self.recovery_seconds:
                    raise CircuitBreakerOpen()
                self._state = CircuitState.HALF_OPEN
                return _Permit(self._generation, True)
            if self._state is CircuitState.HALF_OPEN:
                raise CircuitBreakerOpen()
            return _Permit(self._generation, False)

    def record_success(self, permit: _Permit | None) -> None:
        if permit is None:
            return
        with self._lock:
            if permit.generation != self._generation:
                return
            self._failures = 0
            if permit.probe:
                self._state = CircuitState.CLOSED
                self._generation += 1

    def record_failure(self, permit: _Permit | None) -> None:
        if permit is None:
            return
        with self._lock:
            if permit.generation != self._generation:
                return
            self._failures += 1
            if permit.probe or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                self._generation += 1


@dataclass(frozen=True, slots=True)
class BatchConfig:
    max_items: int = 4
    char_budget: int = 4000
    token_budget: int = 900
    connect_timeout: float = 5.0
    response_timeout: float = 30.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_items <= 64:
            raise ValueError("max_items must be between 1 and 64")
        if self.char_budget < 1 or self.token_budget < 1:
            raise ValueError("batch budgets must be positive")
        if self.connect_timeout <= 0 or self.response_timeout <= 0:
            raise ValueError("timeouts must be positive")


_ChunkT = TypeVar("_ChunkT", bound=EmbeddingChunk)


def batch_chunks(
    chunks: Iterable[_ChunkT], config: BatchConfig = BatchConfig()
) -> tuple[tuple[_ChunkT, ...], ...]:
    """Keep input order and isolate, rather than split, oversized chunks."""
    batches: list[tuple[_ChunkT, ...]] = []
    current: list[_ChunkT] = []
    chars = tokens = 0

    for chunk in chunks:
        if not isinstance(chunk.text, str) or not isinstance(chunk.token_estimate, int):
            raise TypeError("chunk text and token estimate are required")
        if chunk.token_estimate < 0:
            raise ValueError("token estimate cannot be negative")
        if current and (
            len(current) >= config.max_items
            or chars + len(chunk.text) > config.char_budget
            or tokens + chunk.token_estimate > config.token_budget
        ):
            batches.append(tuple(current))
            current = []
            chars = tokens = 0
        current.append(chunk)
        chars += len(chunk.text)
        tokens += chunk.token_estimate

    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _vectors(
    response: EmbeddingResponse, expected_count: int, dimension: int | None
) -> tuple[tuple[float, ...], ...]:
    try:
        if not isinstance(response.model, str) or not response.model.strip():
            raise ValueError
        items = tuple(response.items)
        if len(items) != expected_count:
            raise ValueError
        vectors: list[tuple[float, ...]] = []
        for position, item in enumerate(items):
            if item.index != position:
                raise ValueError
            raw = tuple(item.vector)
            if dimension is not None and len(raw) != dimension:
                raise ValueError
            if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) for value in raw):
                raise ValueError
            vectors.append(tuple(float(value) for value in raw))
        return tuple(vectors)
    except (AttributeError, TypeError, ValueError):
        raise EmbeddingServiceError() from None


def _retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    return status is None or status in _RETRYABLE_STATUS


def _raise_service_error(error: Exception) -> None:
    if isinstance(error, EmbeddingServiceError):
        raise error
    raise EmbeddingServiceError() from error


def _retry_delay(
    error: Exception,
    attempt: int,
    jitter: Callable[[float, float], float],
) -> float:
    delay = min(1.0 * 2**attempt, 30.0) * jitter(0.5, 1.0)
    retry_after = getattr(error, "retry_after_seconds", None)
    if (
        isinstance(retry_after, Real)
        and not isinstance(retry_after, bool)
        and math.isfinite(float(retry_after))
    ):
        delay = max(delay, float(retry_after))
    return delay


def _embed_with_retries(
    transport: EmbeddingTransport,
    request: EmbeddingRequest,
    expected_count: int,
    dimension: int | None,
    *,
    sleep: Callable[[float], None],
    jitter: Callable[[float, float], float],
) -> tuple[tuple[float, ...], ...]:
    for attempt in range(4):
        try:
            return _vectors(transport(request), expected_count, dimension)
        except Exception as error:
            if attempt < 3 and _retryable(error):
                sleep(_retry_delay(error, attempt, jitter))
                continue
            raise
    raise AssertionError("unreachable")


class QueryEmbedder:
    """Single-query embedding with provider token truncation and logical retries."""

    def __init__(
        self,
        transport: EmbeddingTransport,
        *,
        model: str,
        truncate: TokenTruncator,
        dimension: int | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        connect_timeout: float = 5.0,
        response_timeout: float = 5.0,
        max_tokens: int = 8191,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        if dimension is not None and dimension < 1:
            raise ValueError("dimension must be positive")
        if connect_timeout <= 0 or response_timeout <= 0 or max_tokens < 1:
            raise ValueError("timeouts and max_tokens must be positive")
        self._transport = transport
        self._model = model
        self._truncate = truncate
        self._dimension = dimension
        self.breaker = breaker or CircuitBreaker(5, 30.0)
        self._sleep = sleep
        self._jitter = jitter
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._max_tokens = max_tokens

    def embed_query(self, text: str) -> tuple[float, ...]:
        prepared = self._truncate(text, self._max_tokens)
        if not isinstance(prepared, str):
            raise TypeError("token truncator must return text")
        request = EmbeddingRequest(
            self._model,
            (prepared,),
            self._connect_timeout,
            self._response_timeout,
        )
        permit = self.breaker.before_call()

        try:
            result = _embed_with_retries(
                self._transport,
                request,
                1,
                self._dimension,
                sleep=self._sleep,
                jitter=self._jitter,
            )[0]
        except CircuitBreakerOpen:
            self.breaker.record_failure(permit)
            raise
        except Exception as error:
            self.breaker.record_failure(permit)
            _raise_service_error(error)
        else:
            self.breaker.record_success(permit)
            return result


class BatchEmbedder:
    """Retry document batches and return only a complete ordered vector set."""

    def __init__(
        self,
        transport: EmbeddingTransport,
        *,
        model: str,
        dimension: int,
        config: BatchConfig = BatchConfig(),
        breaker: CircuitBreaker | None = None,
        truncate: TokenTruncator | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_tokens: int = 8191,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model is required")
        if dimension < 1 or max_tokens < 1:
            raise ValueError("dimension and max_tokens must be positive")
        self._transport = transport
        self._model = model
        self._dimension = dimension
        self.config = config
        self.breaker = breaker or CircuitBreaker(5, 30.0)
        self._truncate = truncate or (lambda text, _limit: text)
        self._sleep = sleep
        self._jitter = jitter
        self._max_tokens = max_tokens

    def embed(self, chunks: Iterable[EmbeddingChunk]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for batch in batch_chunks(chunks, self.config):
            request = EmbeddingRequest(
                self._model,
                tuple(
                    self._truncate(chunk.text, self._max_tokens) for chunk in batch
                ),
                self.config.connect_timeout,
                self.config.response_timeout,
            )
            permit = self.breaker.before_call()
            try:
                response_vectors = _embed_with_retries(
                    self._transport,
                    request,
                    len(batch),
                    self._dimension,
                    sleep=self._sleep,
                    jitter=self._jitter,
                )
            except CircuitBreakerOpen:
                self.breaker.record_failure(permit)
                raise
            except Exception as error:
                self.breaker.record_failure(permit)
                _raise_service_error(error)
            else:
                self.breaker.record_success(permit)
                vectors.extend(response_vectors)
        return tuple(vectors)
