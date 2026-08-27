"""Synchronous search entrypoints, cache policy, and error translation."""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from src.shared import (
    EmbeddingCircuitOpen,
    EmbeddingUnavailable,
    Principal,
    PublicError,
    Request,
    Response,
    SearchAnswerHistoryRecord,
    SearchCitationHistoryRecord,
    SearchHistoryBundle,
    SearchHistoryRecord,
    LogicalCircuitBreaker,
    SearchUnavailable,
    body_violation,
)

from .calls import GenerationRunner
from .core import DEFAULT_DEFINITION, PipelineInterpreter
from .model import SearchExecution, SearchPorts
from .settings import effective_settings


DEFAULT_IMPLEMENTATION_DEFINITION = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "pipeline"
    / "search.implementation.json"
)


class SearchService:
    def __init__(
        self,
        ports: SearchPorts,
        stored_settings: Mapping[str, object] | Callable[[], Mapping[str, object]] | None = None,
        *,
        definition=DEFAULT_DEFINITION,
        implementation_definition: str | Path = DEFAULT_IMPLEMENTATION_DEFINITION,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if ports.guardrails is None:
            raise ValueError("search guardrails port is required")
        if ports.history is None:
            raise ValueError("search history port is required")
        self.ports = ports
        self._stored_settings = stored_settings or {}
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self.pipeline = PipelineInterpreter(definition, timer=monotonic)
        self.definition = self._effective_definition(implementation_definition)
        self.cache_misses = 0
        self._startup_embedding_model = effective_settings(self._stored())["embedding_model"]
        self.llm_breaker = LogicalCircuitBreaker(5, 30.0, clock=monotonic)
        self.generation_runner = GenerationRunner()
        self._rate_windows: dict[tuple[str, int], int] = {}
        self._rate_lock = RLock()

    def _effective_definition(self, amendment_path: str | Path) -> dict[str, object]:
        amendment = json.loads(Path(amendment_path).read_text(encoding="utf-8"))
        additions = amendment.get("request_override_additions")
        subset = amendment.get("cache_settings_subset")
        if not isinstance(additions, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("field"), str)
            and isinstance(item.get("target"), str)
            for item in additions
        ):
            raise ValueError("invalid search request override amendment")
        if not isinstance(subset, list) or not subset or not all(
            isinstance(item, str) for item in subset
        ) or len(subset) != len(set(subset)):
            raise ValueError("invalid search cache settings amendment")

        definition = dict(self.pipeline.definition)
        base_override = self.pipeline.definition.get("request_override", {})
        override = dict(base_override) if isinstance(base_override, dict) else {}
        rules = list(override.get("rules", ()))
        existing = {
            item.get("field") for item in rules if isinstance(item, dict)
        }
        rules.extend(item for item in additions if item["field"] not in existing)
        override["rules"] = rules
        definition["request_override"] = override

        base_cache = self.pipeline.definition.get("cache", {})
        cache = dict(base_cache) if isinstance(base_cache, dict) else {}
        cache["settings_subset"] = list(subset)
        definition["cache"] = cache
        return definition

    def _stored(self) -> Mapping[str, object]:
        value = self._stored_settings() if callable(self._stored_settings) else self._stored_settings
        return value

    def _settings(self, payload: Mapping[str, object]) -> dict[str, object]:
        settings = effective_settings(self._stored())
        settings["embedding_model"] = self._startup_embedding_model
        override = self.definition.get("request_override", {})
        rules = override.get("rules", ()) if isinstance(override, dict) else ()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            field, target = rule.get("field"), rule.get("target")
            if isinstance(field, str) and isinstance(target, str) and payload.get(field) is not None:
                settings[target] = payload[field]
        for step in self.pipeline.steps:
            modes = step.get("modes")
            if isinstance(modes, dict):
                requested = str(settings.get("search_mode", ""))
                if requested not in modes:
                    settings["search_mode"] = str(step.get("unknown_mode_fallback", "hybrid"))
                break
        return settings

    @staticmethod
    def _validated(payload: object) -> Mapping[str, object]:
        if not isinstance(payload, Mapping):
            body_violation("body", "JSON 객체여야 합니다.")
        if "query" not in payload or not isinstance(payload["query"], str):
            body_violation("query", "필수 문자열이어야 합니다.")
        for field in ("hyde_enabled", "reranking_enabled", "multi_query_enabled"):
            value = payload.get(field)
            if value is not None and not isinstance(value, bool):
                body_violation(field, "boolean 또는 null이어야 합니다.")
        if "generate_answer" in payload and not isinstance(payload["generate_answer"], bool):
            body_violation("generate_answer", "boolean이어야 합니다.")
        top_k = payload.get("top_k")
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int)):
            body_violation("top_k", "정수 또는 null이어야 합니다.")
        mode = payload.get("search_mode")
        if mode is not None and not isinstance(mode, str):
            body_violation("search_mode", "문자열 또는 null이어야 합니다.")
        return payload

    def _settings_hash(self, settings: Mapping[str, object]) -> str:
        cache = self.definition.get("cache", {})
        subset = cache.get("settings_subset", ()) if isinstance(cache, dict) else ()
        selected = {str(key): settings.get(str(key)) for key in subset}
        encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _requester(principal: Principal) -> str:
        return str(principal.user_id if principal.user_id is not None else principal.subject)

    def _cache_key(
        self, requester: str, query: str, settings_hash: str
    ) -> str:
        raw = json.dumps((requester, query, settings_hash), ensure_ascii=False, separators=(",", ":"))
        return "search:" + hashlib.sha256(raw.encode()).hexdigest()

    def _cache_hit(
        self, key: str, principal: Principal
    ) -> dict[str, object] | None:
        if self.ports.cache is None:
            return None
        try:
            raw = self.ports.cache.get(key)
            if raw is None:
                return None
            body = json.loads(raw)
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("document_id"), str)
                or not self.ports.permissions.can_read_document(
                    principal, item["document_id"]
                )
                for item in results
            ):
                self.ports.cache.delete(key)
                return None
            return body
        except Exception:
            return None

    def execute(
        self,
        payload: object,
        principal: Principal,
        *,
        debug: bool = False,
    ) -> SearchExecution:
        request = self._validated(payload)
        query = str(request["query"])
        settings = self._settings(request)
        settings_hash = self._settings_hash(settings)
        requester = self._requester(principal)
        key = self._cache_key(requester, query, settings_hash)
        started = self._monotonic()
        requested_at = self._now()
        status = "SUCCESS"
        results_count = 0
        answer_history: SearchAnswerHistoryRecord | None = None
        citation_history: tuple[SearchCitationHistoryRecord, ...] = ()
        normal_cache = not debug and bool(settings.get("cache_enabled", True))
        try:
            if normal_cache:
                cached = self._cache_hit(key, principal)
                if cached is not None:
                    results_count = len(cached["results"])
                    answer_history = SearchAnswerHistoryRecord(
                        requester,
                        requested_at,
                        str(cached.get("answer", "")),
                        status,
                        settings_hash,
                    )
                    citation_history = tuple(
                        SearchCitationHistoryRecord(
                            requester,
                            requested_at,
                            rank,
                            str(item["chunk_id"]),
                            str(item["document_id"]),
                        )
                        for rank, item in enumerate(cached["results"], 1)
                    )
                    return SearchExecution(cached, (("X-Cache", "HIT"),))
                self.cache_misses += 1

            state = {
                "query": query,
                "answer": "",
                "results": [],
                "ports": self.ports,
                "principal": principal,
                "clock": self._monotonic,
                "sleep": self._sleep,
                "jitter": self._jitter,
                "cacheable": True,
                "llm_breaker": self.llm_breaker,
                "generation_runner": self.generation_runner,
            }
            try:
                run = self.pipeline.run(state, settings=settings)
            except (EmbeddingCircuitOpen,) as error:
                raise PublicError("CIRCUIT_BREAKER_OPEN") from error
            except (EmbeddingUnavailable,) as error:
                raise PublicError("EMBEDDING_SERVICE_ERROR") from error
            except SearchUnavailable as error:
                raise PublicError("SEARCH_SERVICE_ERROR", str(error) or None) from error
            except Exception as error:
                code = getattr(error, "code", None)
                if code in {"CIRCUIT_BREAKER_OPEN", "EMBEDDING_SERVICE_ERROR", "SEARCH_SERVICE_ERROR"}:
                    raise PublicError(code) from error
                raise
            final_state = run["state"]
            public_error = final_state.get("public_error")
            if isinstance(public_error, tuple) and len(public_error) == 2:
                raise PublicError(str(public_error[0]), str(public_error[1]))
            results = [
                {
                    "chunk_id": item["chunk_id"],
                    "document_id": item["document_id"],
                    "content": item["content"],
                    "score": item["score"],
                    "metadata": item.get("metadata"),
                }
                for item in final_state.get("results", [])
            ]
            body: dict[str, object] = {
                "query": query,
                "answer": str(final_state.get("answer", "")),
                "results": results,
            }
            if debug:
                body["pipeline_trace"] = run["pipeline_trace"]
            results_count = len(results)
            answer_history = SearchAnswerHistoryRecord(
                requester,
                requested_at,
                str(final_state.get("answer", "")),
                status,
                settings_hash,
            )
            citation_history = tuple(
                SearchCitationHistoryRecord(
                    requester,
                    requested_at,
                    rank,
                    str(item["chunk_id"]),
                    str(item["document_id"]),
                )
                for rank, item in enumerate(results, 1)
            )
            cacheable = bool(final_state.get("cacheable", True))
            headers = () if debug else (("X-Cache", "MISS"),)
            if normal_cache and cacheable and self.ports.cache is not None:
                try:
                    self.ports.cache.set(
                        key,
                        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                        int(settings.get("cache_search_ttl", 3600)),
                    )
                except Exception:
                    pass
            return SearchExecution(body, headers, cacheable)
        except PublicError as error:
            status = error.code
            raise
        except Exception as error:
            status = str(getattr(error, "code", "COMMON-006"))
            raise
        finally:
            try:
                self.ports.history.record(
                    SearchHistoryBundle(
                        SearchHistoryRecord(
                            requester,
                            query,
                            requested_at,
                            max(0.0, (self._monotonic() - started) * 1000.0),
                            results_count,
                            status,
                            settings_hash,
                        ),
                        answer_history,
                        citation_history,
                    )
                )
            except Exception:
                pass

    def handler(self, *, debug: bool = False):
        def handle(request: Request) -> Response:
            if request.principal is None:
                raise PublicError("COMMON-007")
            if not debug:
                self._check_rate_limit(request.principal)
            try:
                payload = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise PublicError("COMMON-002") from None
            execution = self.execute(payload, request.principal, debug=debug)
            body = json.dumps(
                execution.body, ensure_ascii=False, separators=(",", ":")
            ).encode()
            return Response(
                200,
                body,
                (
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                )
                + execution.headers,
            )

        return handle

    def mount(self, app: object) -> None:
        """Attach both confirmed search entrypoints to the platform router."""

        add_route = getattr(app, "add_route")
        add_route("POST", "/api/search", self.handler())
        add_route("POST", "/api/search/debug", self.handler(debug=True))

    def _check_rate_limit(self, principal: Principal) -> None:
        requester = self._requester(principal)
        bucket = int(self._monotonic() // 60)
        key = (requester, bucket)
        entrypoints = self.definition.get("entrypoints", ())
        limit = next(
            (
                int(item["rate_limit_per_min"])
                for item in entrypoints
                if isinstance(item, dict)
                and item.get("path") == "/api/search"
                and isinstance(item.get("rate_limit_per_min"), int)
            ),
            30,
        )
        with self._rate_lock:
            count = self._rate_windows.get(key, 0)
            if count >= limit:
                raise PublicError("SEARCH_RATE_LIMIT")
            self._rate_windows[key] = count + 1
            if len(self._rate_windows) > 1000:
                self._rate_windows = {
                    window: value
                    for window, value in self._rate_windows.items()
                    if window[1] >= bucket - 1
                }
