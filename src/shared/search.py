"""External adapter contracts used by the synchronous search pipeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
import time
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .access import Identifier


_DOCUMENT_SEARCH_PREFIX = UUID("d0c6d000-0000-5000-8000-000000000000").int
_DOCUMENT_ID_MASK = (1 << 48) - 1
_DOCUMENT_PREFIX_MASK = ((1 << 128) - 1) ^ _DOCUMENT_ID_MASK
_CHUNK_SEARCH_NAMESPACE = uuid5(NAMESPACE_URL, "vectorshelf:search:chunk")


def document_search_id(document_id: Identifier) -> str:
    """Return the stable UUID exposed by search for an integer document ID."""

    if isinstance(document_id, bool):
        raise ValueError("document ID must be a positive integer")
    try:
        value = int(document_id)
    except (TypeError, ValueError) as error:
        raise ValueError("document ID must be a positive integer") from error
    if value < 1 or value > _DOCUMENT_ID_MASK:
        raise ValueError("document ID must be a positive integer")
    return str(UUID(int=_DOCUMENT_SEARCH_PREFIX | value))


def resolve_document_search_id(identifier: Identifier) -> int | None:
    """Resolve either an internal integer ID or its stable search UUID."""

    if isinstance(identifier, bool):
        return None
    try:
        value = int(identifier)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    if not isinstance(identifier, str):
        return None
    try:
        parsed = UUID(identifier)
    except (ValueError, AttributeError):
        return None
    if parsed.int & _DOCUMENT_PREFIX_MASK != _DOCUMENT_SEARCH_PREFIX:
        return None
    internal_id = parsed.int & _DOCUMENT_ID_MASK
    return internal_id or None


def chunk_search_id(version_id: Identifier, chunk_index: int) -> str:
    """Return a stable UUID for one version-local chunk position."""

    if isinstance(chunk_index, bool) or chunk_index < 0:
        raise ValueError("chunk index must be non-negative")
    if isinstance(version_id, bool):
        raise ValueError("version ID must be a positive integer")
    try:
        value = int(version_id)
    except (TypeError, ValueError) as error:
        raise ValueError("version ID must be a positive integer") from error
    if value < 1:
        raise ValueError("version ID must be a positive integer")
    return str(uuid5(_CHUNK_SEARCH_NAMESPACE, f"{value}:{chunk_index}"))


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    document_id: str
    content: str
    score: float
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class LanguageModelRequest:
    task: str
    prompt: str
    model: str
    temperature: float
    timeout_seconds: float
    system_prompt: str | None = None
    provider: str = "openai"


@dataclass(frozen=True, slots=True)
class SearchHistoryRecord:
    requester_id: str
    query: str
    requested_at: datetime
    duration_ms: float
    results_count: int
    status: str
    settings_hash: str


@dataclass(frozen=True, slots=True)
class SearchAnswerHistoryRecord:
    requester_id: str
    requested_at: datetime
    answer: str
    status: str
    settings_hash: str


@dataclass(frozen=True, slots=True)
class SearchCitationHistoryRecord:
    requester_id: str
    requested_at: datetime
    rank: int
    chunk_id: str
    document_id: str


@dataclass(frozen=True, slots=True)
class SearchHistoryBundle:
    search: SearchHistoryRecord
    answer: SearchAnswerHistoryRecord | None = None
    citations: tuple[SearchCitationHistoryRecord, ...] = ()


class VectorSearcher(Protocol):
    def search(
        self,
        vector: Sequence[float],
        document_ids: frozenset[Identifier],
        limit: int,
    ) -> Sequence[SearchHit]: ...


class KeywordSearcher(Protocol):
    def search(
        self,
        query: str,
        document_ids: frozenset[Identifier],
        limit: int,
        *,
        timeout_seconds: float,
    ) -> Sequence[SearchHit]: ...


class LanguageModel(Protocol):
    def complete(self, request: LanguageModelRequest) -> str: ...


class Reranker(Protocol):
    def score(
        self, query: str, contents: Sequence[str], *, model: str
    ) -> Sequence[float]: ...


class SearchHistoryWriter(Protocol):
    def record(self, bundle: SearchHistoryBundle) -> None: ...

    def purge_before(self, cutoff: datetime) -> None:
        """Delete search, answer, and citation history older than ``cutoff``."""
        ...


class SearchCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    def delete(self, key: str) -> None: ...


class GuardrailPort(Protocol):
    """Internal feature boundary; concrete policy remains in guardrails/."""

    def inspect_input(
        self, query: str, llm: LanguageModel, settings: Mapping[str, object], breaker=None
    ): ...

    def mask_results(self, results: Sequence[Mapping[str, object]]): ...

    def verify_numbers(self, answer: str, evidence_texts: Sequence[str]): ...

    def judge_faithfulness(
        self, answer: str, evidence_texts: Sequence[str], llm: LanguageModel,
        settings: Mapping[str, object], breaker=None
    ): ...

    def judge_hallucination(
        self, answer: str, evidence_texts: Sequence[str], llm: LanguageModel,
        settings: Mapping[str, object], breaker=None
    ): ...


class EmbeddingUnavailable(RuntimeError):
    code = "EMBEDDING_SERVICE_ERROR"


class EmbeddingCircuitOpen(RuntimeError):
    code = "CIRCUIT_BREAKER_OPEN"


class SearchUnavailable(RuntimeError):
    code = "SEARCH_SERVICE_ERROR"


class KeywordTransportError(RuntimeError):
    """Connection, timeout, or protocol failure eligible for app retries."""


class GenerationDeadlineExceeded(TimeoutError):
    """The complete answer-generation branch exceeded its 25-second budget."""


@dataclass(frozen=True, slots=True)
class _CallPermit:
    generation: int
    probe: bool


class LogicalCircuitBreaker:
    """Count exhausted logical operations, not their individual retry attempts."""

    def __init__(
        self,
        threshold: int = 5,
        recovery_seconds: float = 30.0,
        *,
        clock=time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe = False
        self._generation = 0
        self._lock = RLock()

    def before_call(self) -> _CallPermit:
        with self._lock:
            if self._opened_at is not None:
                if self._clock() - self._opened_at < self.recovery_seconds or self._probe:
                    raise SearchUnavailable("LLM circuit open")
                self._probe = True
                return _CallPermit(self._generation, True)
            return _CallPermit(self._generation, False)

    def record_success(self, permit: _CallPermit) -> None:
        with self._lock:
            if permit.generation != self._generation:
                return
            self._failures = 0
            self._opened_at = None
            self._probe = False
            if permit.probe:
                self._generation += 1

    def record_failure(self, permit: _CallPermit) -> None:
        with self._lock:
            if permit.generation != self._generation:
                return
            self._failures += 1
            if permit.probe or self._failures >= self.threshold:
                self._opened_at = self._clock()
                self._probe = False
                self._generation += 1
