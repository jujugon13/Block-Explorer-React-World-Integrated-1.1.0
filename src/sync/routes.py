"""ADMIN-only REST handlers for synchronization operations."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from uuid import UUID

from src.shared import Identifier, PublicError, Request, body_violation

from .consistency import ISSUE_TYPES, SEVERITIES
from .core import SyncService


EVENT_STATUSES = frozenset({"PENDING", "PROCESSING", "PROCESSED", "FAILED"})
EVENT_TYPES = frozenset(
    {
        "DOCUMENT_VERSION_CREATED",
        "DOCUMENT_REINDEX_REQUESTED",
        "DOCUMENT_DELETED",
        "PERMISSION_CACHE_REFRESH_REQUESTED",
        "EMBEDDING_MODEL_ACTIVATED",
    }
)
ISSUE_STATUSES = frozenset({"OPEN", "REPAIRING", "RESOLVED", "IGNORED"})


def _admin(request: Request) -> Identifier:
    principal = request.principal
    if principal is None:
        raise PublicError("COMMON-007")
    if "ADMIN" not in principal.roles:
        raise PublicError("ROLE-002")
    return principal.user_id if principal.user_id is not None else principal.subject


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


def _query(request: Request) -> Mapping[str, object]:
    value = getattr(request, "query_params", {})
    return value if isinstance(value, Mapping) else {}


def _one(value: object | None) -> object | None:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _page(query: Mapping[str, object]) -> tuple[int, int]:
    try:
        page = int(_one(query.get("page", 0)))
        size = int(_one(query.get("size", 20)))
    except (TypeError, ValueError):
        raise PublicError("COMMON-002") from None
    if page < 0 or not 1 <= size <= 100:
        raise PublicError("COMMON-002")
    return page, size


def _enum(
    query: Mapping[str, object],
    name: str,
    allowed: frozenset[str],
) -> str | None:
    value = _one(query.get(name))
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise PublicError("COMMON-002")
    return value


def _uuid_path(request: Request, name: str) -> str:
    value = request.path_params.get(name)
    try:
        return str(UUID(value)) if value is not None else ""
    except (TypeError, ValueError, AttributeError):
        raise PublicError("COMMON-002") from None


def _paged(content: tuple[dict[str, object], ...], page: int, size: int) -> dict[str, object]:
    total = len(content)
    total_pages = math.ceil(total / size) if total else 0
    selected = content[page * size : (page + 1) * size]
    return {
        "content": list(selected),
        "page": page,
        "size": size,
        "totalElements": total,
        "totalPages": total_pages,
        "first": page == 0,
        "last": page >= max(0, total_pages - 1),
    }


class SyncAdminApi:
    """Minimal response projections for the seven confirmed sync endpoints."""

    def __init__(self, service: SyncService) -> None:
        self.service = service

    def summary(self, request: Request) -> dict[str, object]:
        _admin(request)
        return self.service.summary()

    def events(self, request: Request) -> dict[str, object]:
        _admin(request)
        query = _query(request)
        page, size = _page(query)
        status = _enum(query, "status", EVENT_STATUSES)
        event_type = _enum(query, "eventType", EVENT_TYPES)
        rows = self.service.events(status=status, event_type=event_type)
        content = tuple(
            {
                "eventId": row.id,
                "status": row.status,
                "eventType": row.event_type,
            }
            for row in rows
        )
        return _paged(content, page, size)

    def issues(self, request: Request) -> dict[str, object]:
        _admin(request)
        query = _query(request)
        page, size = _page(query)
        status = _enum(query, "status", ISSUE_STATUSES)
        issue_type = _enum(query, "issueType", ISSUE_TYPES)
        severity = _enum(query, "severity", SEVERITIES)
        rows = self.service.issues(
            status=status,
            issue_type=issue_type,
            severity=severity,
        )
        content = tuple(
            {
                "issueId": row.id,
                "status": row.status,
                "issueType": row.issue_type,
                "severity": row.severity,
            }
            for row in rows
        )
        return _paged(content, page, size)

    def retry_event(self, request: Request) -> dict[str, object]:
        actor_id = _admin(request)
        event = self.service.retry_failed(
            _uuid_path(request, "eventId"),
            actor_id=actor_id,
        )
        return {"eventId": event.id, "status": event.status}

    def repair_issue(self, request: Request) -> dict[str, object]:
        actor_id = _admin(request)
        issue = self.service.repair_issue(
            _uuid_path(request, "issueId"),
            actor_id=actor_id,
        )
        return {"issueId": issue.id, "status": issue.status}

    def ignore_issue(self, request: Request) -> dict[str, object]:
        actor_id = _admin(request)
        body = _body(request)
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            body_violation("reason", "필수 문자열이며 공백일 수 없습니다.")
        issue = self.service.ignore_issue(
            _uuid_path(request, "issueId"),
            reason,
            actor_id=actor_id,
        )
        return {"issueId": issue.id, "status": issue.status}

    def reconcile(self, request: Request) -> dict[str, object]:
        actor_id = _admin(request)
        body = _body(request)
        cursor = body.get("cursor")
        mode = body.get("mode", "DRY_RUN")
        if cursor is not None and not isinstance(cursor, str):
            body_violation("cursor", "문자열 또는 null이어야 합니다.")
        if not isinstance(mode, str):
            body_violation("mode", "문자열이어야 합니다.")
        if mode not in {"DRY_RUN", "REPAIR"}:
            body_violation("mode", "DRY_RUN 또는 REPAIR이어야 합니다.")
        run = self.service.reconcile(
            cursor=cursor,
            mode=mode,
            actor_id=actor_id,
        )
        return {"reconciliationId": run.id, "status": run.status}


def register_sync_routes(app: object, service: SyncService) -> SyncAdminApi:
    api = SyncAdminApi(service)
    app.add_route("GET", "/admin/sync/summary", api.summary)
    app.add_route("GET", "/admin/sync/events", api.events)
    app.add_route("GET", "/admin/sync/issues", api.issues)
    app.add_route(
        "POST",
        "/admin/sync/events/{eventId}/retry",
        api.retry_event,
    )
    app.add_route(
        "POST",
        "/admin/sync/issues/{issueId}/repair",
        api.repair_issue,
    )
    app.add_route(
        "POST",
        "/admin/sync/issues/{issueId}/ignore",
        api.ignore_issue,
    )
    app.add_route("POST", "/admin/sync/reconcile", api.reconcile)
    return api
