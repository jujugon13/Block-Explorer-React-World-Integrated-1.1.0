"""ADMIN REST boundary for indexing jobs and workers."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TypeAlias
from uuid import UUID

from src.shared import ChunkRecord, PublicError, Request, body_violation

from .core import IndexingService
from .model import ModelRow, OperationResult


JOB_STATUSES = frozenset({"PENDING", "PROCESSING", "INDEXED", "FAILED"})
JOB_FIELDS = (
    "jobId",
    "documentId",
    "documentVersionId",
    "status",
    "priority",
    "retryCount",
    "maxRetries",
    "workerId",
    "lockedAt",
    "leaseExpiresAt",
    "firstStartedAt",
    "nextRunAt",
    "completedAt",
    "failedAt",
    "failureType",
    "errorMessage",
)
ATTEMPT_FIELDS = (
    "attemptId",
    "jobId",
    "attemptNo",
    "workerId",
    "status",
    "startedAt",
    "endedAt",
    "durationMs",
    "failureType",
    "errorMessage",
)
EVENT_FIELDS = ("eventId", "jobId", "eventType", "occurredAt")
WORKER_FIELDS = ("workerId", "instanceId", "name", "status", "lastHeartbeat")
CLAIM_FIELDS = (
    "jobId",
    "workerId",
    "documentVersionId",
    "status",
    "claimToken",
    "lockedAt",
    "leaseExpiresAt",
)
RENEW_FIELDS = ("jobId", "leaseExpiresAt")
CHUNK_FIELDS = ("documentVersionId", "chunkCount")
EMBEDDING_FIELDS = (
    "documentVersionId",
    "embeddingModelId",
    "embeddingCount",
)
COMPLETE_FIELDS = (
    "jobId",
    "attemptId",
    "documentVersionId",
    "status",
    "completedAt",
)
FAILURE_FIELDS = (
    "jobId",
    "attemptId",
    "status",
    "retryCount",
    "nextRunAt",
    "failureType",
    "errorMessage",
)

ChunkProducer: TypeAlias = Callable[[int, int, int, str], Iterable[object]]
EmbeddingProducer: TypeAlias = Callable[
    [tuple[ChunkRecord, ...], ModelRow], Iterable[Sequence[float]]
]


def _admin(request: Request) -> None:
    principal = request.principal
    if principal is None:
        raise PublicError("COMMON-007")
    if "ADMIN" not in principal.roles:
        raise PublicError("ROLE-002")


def _body(request: Request) -> Mapping[str, object]:
    if not request.body:
        return {}
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicError("COMMON-002") from None
    if not isinstance(value, dict):
        body_violation("body", "JSON 객체여야 합니다.")
    return value


def _one(value: object | None) -> object | None:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _query(request: Request) -> Mapping[str, object]:
    value = getattr(request, "query_params", {})
    return value if isinstance(value, Mapping) else {}


def _positive_int(
    value: object, *, body: bool = False, field: str = "body"
) -> int:
    if isinstance(value, bool) or (body and not isinstance(value, int)):
        if body:
            body_violation(field, "1 이상의 정수여야 합니다.")
        raise PublicError("COMMON-002")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        if body:
            body_violation(field, "1 이상의 정수여야 합니다.")
        raise PublicError("COMMON-002") from None
    if parsed <= 0:
        if body:
            body_violation(field, "1 이상의 정수여야 합니다.")
        raise PublicError("COMMON-002")
    return parsed


def _path_id(request: Request, name: str) -> int:
    try:
        value = request.path_params[name]
    except KeyError:
        raise PublicError("COMMON-002") from None
    return _positive_int(value)


def _query_id(query: Mapping[str, object], name: str) -> int | None:
    value = _one(query.get(name))
    return None if value is None else _positive_int(value)


def _claim_token(value: object) -> str:
    if not isinstance(value, str) or not value:
        body_violation("claimToken", "필수 UUID 문자열이어야 합니다.")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        body_violation("claimToken", "UUID 문자열이어야 합니다.")


def _ownership(request: Request) -> tuple[int, str, Mapping[str, object]]:
    body = _body(request)
    if "workerId" not in body:
        body_violation("workerId", "필수 필드입니다.")
    if "claimToken" not in body:
        body_violation("claimToken", "필수 필드입니다.")
    worker_id = _positive_int(body["workerId"], body=True, field="workerId")
    token = _claim_token(body["claimToken"])
    return worker_id, token, body


def _page(query: Mapping[str, object]) -> tuple[int, int]:
    page_value = _one(query.get("page", "0"))
    size_value = _one(query.get("size", "20"))
    if isinstance(page_value, bool) or isinstance(size_value, bool):
        raise PublicError("COMMON-002")
    try:
        page = int(page_value)
        size = int(size_value)
    except (TypeError, ValueError, OverflowError):
        raise PublicError("COMMON-002") from None
    if page < 0 or not 1 <= size <= 100:
        raise PublicError("COMMON-002")
    return page, size


def _status(query: Mapping[str, object]) -> str | None:
    value = _one(query.get("status"))
    if value is None:
        return None
    if not isinstance(value, str) or value not in JOB_STATUSES:
        raise PublicError("COMMON-002")
    return value


def _paged(
    rows: tuple[dict[str, object], ...], page: int, size: int
) -> dict[str, object]:
    total = len(rows)
    total_pages = math.ceil(total / size) if total else 0
    return {
        "content": list(rows[page * size : (page + 1) * size]),
        "page": page,
        "size": size,
        "totalElements": total,
        "totalPages": total_pages,
        "first": page == 0,
        "last": page >= max(0, total_pages - 1),
    }


def _project(
    value: Mapping[str, object], fields: tuple[str, ...]
) -> dict[str, object]:
    return {field: value[field] for field in fields}


def _project_result(
    result: OperationResult, fields: tuple[str, ...]
) -> OperationResult:
    if result.data is None:
        return OperationResult(result.status)
    return OperationResult(result.status, _project(result.data, fields))


class IndexingAdminApi:
    """Thin HTTP projection over the indexing service's public operations."""

    def __init__(
        self,
        service: IndexingService,
        *,
        chunk_producer: ChunkProducer,
        embedder: EmbeddingProducer,
    ) -> None:
        self.service = service
        self.chunk_producer = chunk_producer
        self.embedder = embedder

    def jobs(self, request: Request) -> dict[str, object]:
        _admin(request)
        query = _query(request)
        page, size = _page(query)
        rows = self.service.jobs(
            status=_status(query),
            document_id=_query_id(query, "documentId"),
            worker_id=_query_id(query, "workerId"),
        )
        return _paged(tuple(_project(row, JOB_FIELDS) for row in rows), page, size)

    def detail(self, request: Request) -> dict[str, object]:
        _admin(request)
        return _project(
            self.service.detail(_path_id(request, "jobId")), JOB_FIELDS
        )

    def attempts(self, request: Request) -> list[dict[str, object]]:
        _admin(request)
        return [
            _project(row, ATTEMPT_FIELDS)
            for row in self.service.attempts(_path_id(request, "jobId"))
        ]

    def events(self, request: Request) -> list[dict[str, object]]:
        _admin(request)
        rows = self.service.events(_path_id(request, "jobId"))
        return [_project(row, EVENT_FIELDS) for row in rows]

    def claim(self, request: Request) -> OperationResult:
        _admin(request)
        worker_id = _query_id(_query(request), "workerId")
        if worker_id is None:
            raise PublicError("COMMON-002")
        return _project_result(self.service.claim(worker_id), CLAIM_FIELDS)

    def renew(self, request: Request) -> OperationResult:
        _admin(request)
        job_id = _path_id(request, "jobId")
        worker_id, token, _ = _ownership(request)
        return _project_result(
            self.service.renew(job_id, worker_id, token), RENEW_FIELDS
        )

    def start_attempt(self, request: Request) -> OperationResult:
        _admin(request)
        job_id = _path_id(request, "jobId")
        worker_id, token, _ = _ownership(request)
        return _project_result(
            self.service.start_attempt(job_id, worker_id, token), ATTEMPT_FIELDS
        )

    def save_chunks(self, request: Request) -> OperationResult:
        _admin(request)
        job_id = _path_id(request, "jobId")
        attempt_id = _path_id(request, "attemptId")
        worker_id, token, _ = _ownership(request)
        return _project_result(
            self.service.save_chunks(
                job_id,
                attempt_id,
                worker_id,
                token,
                creator=lambda: self.chunk_producer(
                    job_id, attempt_id, worker_id, token
                ),
            ),
            CHUNK_FIELDS,
        )

    def save_embeddings(self, request: Request) -> OperationResult:
        _admin(request)
        job_id = _path_id(request, "jobId")
        attempt_id = _path_id(request, "attemptId")
        worker_id, token, _ = _ownership(request)
        return _project_result(
            self.service.save_embeddings(
                job_id,
                attempt_id,
                worker_id,
                token,
                self.embedder,
            ),
            EMBEDDING_FIELDS,
        )

    def complete(self, request: Request) -> OperationResult:
        _admin(request)
        job_id = _path_id(request, "jobId")
        attempt_id = _path_id(request, "attemptId")
        worker_id, token, _ = _ownership(request)
        return _project_result(
            self.service.complete(job_id, attempt_id, worker_id, token),
            COMPLETE_FIELDS,
        )

    def fail(self, request: Request) -> OperationResult:
        _admin(request)
        job_id = _path_id(request, "jobId")
        attempt_id = _path_id(request, "attemptId")
        worker_id, token, body = _ownership(request)
        failure_type = body.get("failureType")
        error_message = body.get("errorMessage")
        retry_after = body.get("retryAfter")
        if not isinstance(failure_type, str) or not failure_type:
            body_violation("failureType", "필수 문자열이며 공백일 수 없습니다.")
        if not isinstance(error_message, str):
            body_violation("errorMessage", "필수 문자열이어야 합니다.")
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, (int, float))
            or not math.isfinite(retry_after)
            or retry_after < 0
        ):
            body_violation("retryAfter", "0 이상의 유한한 숫자이어야 합니다.")
        return _project_result(
            self.service.fail(
                job_id,
                attempt_id,
                worker_id,
                token,
                failure_type,
                error_message,
                retry_after=retry_after,
            ),
            FAILURE_FIELDS,
        )

    def retry(self, request: Request) -> OperationResult:
        _admin(request)
        return _project_result(
            self.service.manual_retry(_path_id(request, "jobId")), JOB_FIELDS
        )

    def workers(self, request: Request) -> list[dict[str, object]]:
        _admin(request)
        return [_project(row, WORKER_FIELDS) for row in self.service.workers()]

    def mount(self, app: object) -> None:
        add_route = getattr(app, "add_route")
        add_route("GET", "/admin/indexing-jobs", self.jobs)
        add_route("GET", "/admin/indexing-jobs/{jobId}", self.detail)
        add_route("GET", "/admin/indexing-jobs/{jobId}/attempts", self.attempts)
        add_route("GET", "/admin/indexing-jobs/{jobId}/events", self.events)
        add_route("POST", "/admin/indexing-jobs/claim", self.claim)
        add_route("POST", "/admin/indexing-jobs/{jobId}/lease/renew", self.renew)
        add_route("POST", "/admin/indexing-jobs/{jobId}/attempts", self.start_attempt)
        add_route(
            "POST",
            "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/chunks",
            self.save_chunks,
        )
        add_route(
            "POST",
            "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/embeddings",
            self.save_embeddings,
        )
        add_route(
            "POST",
            "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/complete",
            self.complete,
        )
        add_route(
            "POST",
            "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/fail",
            self.fail,
        )
        add_route("POST", "/admin/indexing-jobs/{jobId}/retry", self.retry)
        add_route("GET", "/admin/workers", self.workers)


def register_indexing_routes(
    app: object,
    service: IndexingService,
    *,
    chunk_producer: ChunkProducer,
    embedder: EmbeddingProducer,
) -> IndexingAdminApi:
    api = IndexingAdminApi(
        service,
        chunk_producer=chunk_producer,
        embedder=embedder,
    )
    api.mount(app)
    return api
