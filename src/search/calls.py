"""Retry policies at the injected external-call boundaries."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread

from src.shared import (
    GenerationDeadlineExceeded,
    KeywordSearcher,
    KeywordTransportError,
    LanguageModel,
    LanguageModelRequest,
    LogicalCircuitBreaker,
    SearchUnavailable,
)


_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})


class GenerationRunner:
    """Run at most one answer-generation call, including timed-out calls."""

    def __init__(self) -> None:
        self._slot = BoundedSemaphore(1)

    @staticmethod
    def _cancel(llm: LanguageModel, request: LanguageModelRequest) -> None:
        cancel = getattr(llm, "cancel", None)
        if callable(cancel):
            try:
                cancel(request)
            except Exception:
                pass

    def complete(
        self,
        llm: LanguageModel,
        request: LanguageModelRequest,
        deadline_at: float,
        clock: Callable[[], float],
    ) -> str:
        remaining = deadline_at - clock()
        if remaining <= 0 or not self._slot.acquire(timeout=remaining):
            raise GenerationDeadlineExceeded

        if clock() >= deadline_at:
            self._slot.release()
            raise GenerationDeadlineExceeded

        outcome: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                try:
                    outcome.put((True, llm.complete(request)))
                except Exception as error:
                    outcome.put((False, error))
            finally:
                self._slot.release()

        try:
            Thread(target=invoke, name="vectorshelf-llm-deadline", daemon=True).start()
        except Exception:
            self._slot.release()
            raise
        remaining = deadline_at - clock()
        if remaining <= 0:
            self._cancel(llm, request)
            raise GenerationDeadlineExceeded
        try:
            succeeded, value = outcome.get(timeout=remaining)
        except Empty:
            self._cancel(llm, request)
            raise GenerationDeadlineExceeded from None
        if not succeeded:
            raise value  # type: ignore[misc]
        return value  # type: ignore[return-value]


_DEFAULT_GENERATION_RUNNER = GenerationRunner()


def llm_complete(
    llm: LanguageModel,
    *,
    task: str,
    prompt: str,
    settings: Mapping[str, object],
    model_key: str = "llm_model",
    deadline_at: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
    breaker: LogicalCircuitBreaker | None = None,
    generation_runner: GenerationRunner | None = None,
) -> str:
    permit = breaker.before_call() if breaker is not None else None
    last: Exception | None = None
    try:
        for attempt in range(4):
            remaining = 30.0 if deadline_at is None else deadline_at - clock()
            if remaining <= 0:
                raise GenerationDeadlineExceeded
            request = LanguageModelRequest(
                task=task,
                prompt=prompt,
                model=str(settings.get(model_key, settings.get("llm_model", ""))),
                temperature=float(settings.get("llm_temperature", 0.3)),
                timeout_seconds=remaining,
                system_prompt=str(settings.get("system_prompt")) or None,
                provider=str(settings.get("llm_provider", "openai")),
            )
            try:
                value = (
                    llm.complete(request)
                    if deadline_at is None
                    else (generation_runner or _DEFAULT_GENERATION_RUNNER).complete(
                        llm,
                        request,
                        deadline_at,
                        clock,
                    )
                )
                if not isinstance(value, str):
                    raise TypeError("LLM response must be text")
                if deadline_at is not None and clock() >= deadline_at:
                    raise GenerationDeadlineExceeded
                if breaker is not None and permit is not None:
                    breaker.record_success(permit)
                return value
            except GenerationDeadlineExceeded:
                raise
            except SearchUnavailable:
                raise
            except TimeoutError as error:
                if deadline_at is not None and clock() >= deadline_at:
                    raise GenerationDeadlineExceeded from error
                last = error
            except Exception as error:
                status = getattr(error, "status_code", None)
                if status is not None and status not in _RETRYABLE_STATUS:
                    raise SearchUnavailable from error
                last = error
            if attempt < 3:
                delay = min(1.0 * 2**attempt, 30.0) * jitter(0.5, 1.0)
                retry_after = getattr(last, "retry_after_seconds", None)
                if isinstance(retry_after, (int, float)) and not isinstance(
                    retry_after, bool
                ) and math.isfinite(float(retry_after)):
                    delay = max(delay, float(retry_after))
                if deadline_at is not None:
                    delay = min(delay, max(0.0, deadline_at - clock()))
                sleep(delay)
        raise SearchUnavailable from last
    except Exception:
        if breaker is not None and permit is not None:
            breaker.record_failure(permit)
        raise


def keyword_search(
    adapter: KeywordSearcher,
    query: str,
    document_ids,
    limit: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
):
    last: Exception | None = None
    for attempt in range(4):
        try:
            return adapter.search(query, document_ids, limit, timeout_seconds=30.0)
        except (ConnectionError, TimeoutError, KeywordTransportError) as error:
            last = error
            if attempt < 3:
                sleep(min(1.0 * 2**attempt, 15.0) * jitter(0.5, 1.0))
        except Exception as error:
            raise SearchUnavailable from error
    raise SearchUnavailable from last
