"""HTTP boundary for the nine confirmed collection endpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping

from src.shared import Principal, PublicError, Request, body_violation

from ..core import CREATE_VISIBILITIES, Collection, CollectionWorkspace


def _principal(request: Request) -> Principal:
    if request.principal is None:
        raise PublicError("COMMON-007")
    return request.principal


def _body(request: Request, allowed: frozenset[str]) -> Mapping[str, object]:
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


def _path_id(request: Request, name: str) -> int:
    try:
        value = int(request.path_params[name])
    except (KeyError, TypeError, ValueError):
        raise PublicError("COMMON-002") from None
    if value <= 0:
        raise PublicError("COMMON-002")
    return value


def _body_id(field: str, value: object, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        body_violation(field, "1 이상의 정수여야 합니다.")
    return value


def _collection(item: Collection) -> dict[str, object]:
    return {
        "collectionId": item.id,
        "name": item.name,
        "ownerUserId": item.owner_user_id,
        "parentId": item.parent_id,
        "visibility": item.visibility,
        "status": item.status,
    }


class CollectionApi:
    def __init__(self, workspace: CollectionWorkspace) -> None:
        self.workspace = workspace

    def create(self, request: Request) -> dict[str, object]:
        body = _body(request, frozenset({"name", "parentId", "visibility"}))
        name = body.get("name")
        visibility = body.get("visibility")
        if not isinstance(name, str) or not name.strip():
            body_violation("name", "필수 문자열이며 공백일 수 없습니다.")
        if visibility is not None and not isinstance(visibility, str):
            body_violation("visibility", "문자열이어야 합니다.")
        if visibility is not None and visibility not in CREATE_VISIBILITIES:
            body_violation(
                "visibility",
                "PRIVATE, COLLECTION, DEPARTMENT, PUBLIC 중 하나여야 합니다.",
            )
        parent_id = _body_id("parentId", body.get("parentId"), optional=True)
        return _collection(
            self.workspace.create(
                _principal(request),
                name,
                parent_id=parent_id,
                visibility=visibility,
            )
        )

    def list(self, request: Request) -> dict[str, object]:
        query = request.query_params
        if set(query) - {"keyword", "page", "size"}:
            raise PublicError("COMMON-002")
        try:
            page = int(query.get("page", "0"))
            size = int(query.get("size", "20"))
        except (TypeError, ValueError):
            raise PublicError("COMMON-002") from None
        keyword = query.get("keyword")
        if keyword is not None and not isinstance(keyword, str):
            raise PublicError("COMMON-002")
        result = self.workspace.list(
            _principal(request), keyword=keyword or None, page=page, size=size
        )
        return {**result, "content": [_collection(item) for item in result["content"]]}

    def get(self, request: Request) -> dict[str, object]:
        return _collection(
            self.workspace.get(_principal(request), _path_id(request, "collectionId"))
        )

    def children(self, request: Request) -> list[dict[str, object]]:
        rows = self.workspace.children(
            _principal(request), _path_id(request, "collectionId")
        )
        return [_collection(item) for item in rows]

    def documents(self, request: Request) -> list[dict[str, object]]:
        rows = self.workspace.documents(
            _principal(request), _path_id(request, "collectionId")
        )
        return [{"documentId": document_id} for document_id in rows]

    def add_document(self, request: Request) -> None:
        body = _body(request, frozenset({"documentId"}))
        document_id = _body_id("documentId", body.get("documentId"))
        self.workspace.add_document(
            _principal(request),
            _path_id(request, "collectionId"),
            document_id,
        )

    def remove_document(self, request: Request) -> None:
        self.workspace.remove_document(
            _principal(request),
            _path_id(request, "collectionId"),
            _path_id(request, "documentId"),
        )

    def update_visibility(self, request: Request) -> None:
        body = _body(request, frozenset({"visibility"}))
        visibility = body.get("visibility")
        if not isinstance(visibility, str):
            body_violation("visibility", "필수 문자열이어야 합니다.")
        self.workspace.update_visibility(
            _principal(request),
            _path_id(request, "collectionId"),
            visibility,
        )

    def delete(self, request: Request) -> None:
        self.workspace.delete(
            _principal(request), _path_id(request, "collectionId")
        )


def register_collection_routes(app: object, workspace: CollectionWorkspace) -> CollectionApi:
    api = CollectionApi(workspace)
    add_route = getattr(app, "add_route")
    add_route("POST", "/collections", api.create, success_status=201)
    add_route("GET", "/collections", api.list)
    add_route("GET", "/collections/{collectionId}", api.get)
    add_route("GET", "/collections/{collectionId}/children", api.children)
    add_route("GET", "/collections/{collectionId}/documents", api.documents)
    add_route(
        "POST",
        "/collections/{collectionId}/documents",
        api.add_document,
        success_status=201,
    )
    add_route(
        "DELETE",
        "/collections/{collectionId}/documents/{documentId}",
        api.remove_document,
        success_status=204,
    )
    add_route(
        "PATCH",
        "/collections/{collectionId}/visibility",
        api.update_visibility,
        success_status=204,
    )
    add_route(
        "DELETE",
        "/collections/{collectionId}",
        api.delete,
        success_status=204,
    )
    return api
