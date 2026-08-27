"""HTTP boundary for the nine confirmed permission endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from src.shared import Principal, PublicError, Request, body_violation

from ..core import (
    PERMISSION_KINDS,
    DirectPermission,
    EffectivePermission,
    PermissionService,
)

UserSearch = Callable[[str], Iterable[Mapping[str, object]]]


def _principal(request: Request) -> Principal:
    if request.principal is None:
        raise PublicError("COMMON-007")
    return request.principal


def _path_id(request: Request, name: str) -> int:
    try:
        value = int(request.path_params[name])
    except (KeyError, TypeError, ValueError):
        raise PublicError("COMMON-002") from None
    if value <= 0:
        raise PublicError("COMMON-002")
    return value


def _body(request: Request) -> Mapping[str, object]:
    allowed = {
        "permissionKind",
        "targetType",
        "userId",
        "departmentId",
        "roleCode",
        "expiresAt",
    }
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicError("COMMON-002") from None
    if not isinstance(value, dict):
        body_violation("body", "JSON 객체여야 합니다.")
    unknown = next((field for field in value if field not in allowed), None)
    if unknown is not None:
        body_violation(unknown, "허용되지 않은 필드입니다.")
    return value


def _optional_id(field: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        body_violation(field, "1 이상의 정수여야 합니다.")
    return value


def _expiration(field: str, value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        body_violation(field, "ISO-8601 날짜시간 문자열이어야 합니다.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        body_violation(field, "ISO-8601 날짜시간 문자열이어야 합니다.")


def _direct(item: DirectPermission) -> dict[str, object]:
    return {
        "permissionId": item.permission_id,
        "permissionKind": item.permission_kind,
        "targetType": item.target_type,
        "userId": item.user_id,
        "departmentId": item.department_id,
        "roleCode": item.role_code,
        "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
    }


def _effective(item: EffectivePermission) -> dict[str, object]:
    return {
        "canRead": item.can_read,
        "canWrite": item.can_write,
        "canAdmin": item.can_admin,
        "sources": list(item.sources),
    }


def _user(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "userId": item.get("userId"),
        "email": item.get("email"),
        "name": item.get("name"),
        "departmentId": item.get("departmentId"),
        "departmentName": item.get("departmentName"),
    }


class PermissionApi:
    def __init__(
        self,
        service: PermissionService,
        *,
        user_search: UserSearch | None = None,
    ) -> None:
        self.service = service
        self.user_search = user_search or (lambda keyword: ())

    def document_me(self, request: Request) -> dict[str, object]:
        return _effective(
            self.service.effective_document(
                _principal(request), _path_id(request, "documentId")
            )
        )

    def list_document(self, request: Request) -> list[dict[str, object]]:
        return self._list(request, "DOCUMENT", "documentId")

    def list_collection(self, request: Request) -> list[dict[str, object]]:
        return self._list(request, "COLLECTION", "collectionId")

    def _list(
        self, request: Request, resource_kind: str, path_name: str
    ) -> list[dict[str, object]]:
        rows = self.service.list_direct(
            _principal(request), resource_kind, _path_id(request, path_name)
        )
        return [_direct(item) for item in rows]

    def document_users(self, request: Request) -> list[Mapping[str, object]]:
        return self._users(request, "DOCUMENT", "documentId")

    def collection_users(self, request: Request) -> list[Mapping[str, object]]:
        return self._users(request, "COLLECTION", "collectionId")

    def _users(
        self, request: Request, resource_kind: str, path_name: str
    ) -> list[Mapping[str, object]]:
        if set(request.query_params) != {"keyword"}:
            raise PublicError("COMMON-002")
        keyword = request.query_params.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 100:
            raise PublicError("COMMON-002")
        self.service.list_direct(
            _principal(request), resource_kind, _path_id(request, path_name)
        )
        return [_user(item) for item in self.user_search(keyword)]

    def grant_document(self, request: Request) -> dict[str, object]:
        return self._grant(request, "DOCUMENT", "documentId")

    def grant_collection(self, request: Request) -> dict[str, object]:
        return self._grant(request, "COLLECTION", "collectionId")

    def _grant(
        self, request: Request, resource_kind: str, path_name: str
    ) -> dict[str, object]:
        body = _body(request)
        permission_kind = body.get("permissionKind")
        target_type = body.get("targetType")
        role_code = body.get("roleCode")
        if not isinstance(permission_kind, str):
            body_violation("permissionKind", "필수 문자열이어야 합니다.")
        if permission_kind not in PERMISSION_KINDS:
            body_violation("permissionKind", "READ, WRITE, ADMIN 중 하나여야 합니다.")
        if not isinstance(target_type, str):
            body_violation("targetType", "필수 문자열이어야 합니다.")
        user_id = _optional_id("userId", body.get("userId"))
        department_id = _optional_id("departmentId", body.get("departmentId"))
        if role_code is not None and not isinstance(role_code, str):
            body_violation("roleCode", "문자열이어야 합니다.")
        expires_at = _expiration("expiresAt", body.get("expiresAt"))
        permission = self.service.grant(
            _principal(request),
            resource_kind,
            _path_id(request, path_name),
            permission_kind,
            target_type=target_type,
            user_id=user_id,
            department_id=department_id,
            role_code=role_code,
            expires_at=expires_at,
        )
        return _direct(permission)

    def revoke_document(self, request: Request) -> None:
        self._revoke(request, "DOCUMENT", "documentId")

    def revoke_collection(self, request: Request) -> None:
        self._revoke(request, "COLLECTION", "collectionId")

    def _revoke(self, request: Request, resource_kind: str, path_name: str) -> None:
        self.service.revoke(
            _principal(request),
            resource_kind,
            _path_id(request, path_name),
            _path_id(request, "permissionId"),
        )


def register_permission_routes(
    app: object,
    service: PermissionService,
    *,
    user_search: UserSearch | None = None,
) -> PermissionApi:
    api = PermissionApi(service, user_search=user_search)
    add_route = getattr(app, "add_route")
    # Literal descendants precede the generic permission-id route because the
    # platform router resolves the first matching template.
    add_route("GET", "/permissions/documents/{documentId}/me", api.document_me)
    add_route("GET", "/permissions/documents/{documentId}", api.list_document)
    add_route("GET", "/permissions/collections/{collectionId}", api.list_collection)
    add_route("GET", "/permissions/documents/{documentId}/users", api.document_users)
    add_route("GET", "/permissions/collections/{collectionId}/users", api.collection_users)
    add_route(
        "POST",
        "/permissions/collections/{collectionId}",
        api.grant_collection,
        success_status=201,
    )
    add_route(
        "POST",
        "/permissions/documents/{documentId}",
        api.grant_document,
        success_status=201,
    )
    add_route(
        "DELETE",
        "/permissions/collections/{collectionId}/{permissionId}",
        api.revoke_collection,
        success_status=204,
    )
    add_route(
        "DELETE",
        "/permissions/documents/{documentId}/{permissionId}",
        api.revoke_document,
        success_status=204,
    )
    return api
