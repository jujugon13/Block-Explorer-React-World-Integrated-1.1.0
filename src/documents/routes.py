"""Public document REST boundary from §5.1.2."""

from __future__ import annotations

import json
from collections.abc import Mapping
from email import policy
from email.parser import BytesParser

from src.shared import Principal, PublicError, Request, body_violation

from .core import DocumentWorkspace
from .model import UploadFile


DOCUMENT_STATUSES = frozenset(
    {"DRAFT", "UPLOADED", "INDEXING", "INDEXED", "FAILED", "ARCHIVED", "DELETED"}
)


def _principal(request: Request) -> Principal:
    if request.principal is None:
        raise PublicError("COMMON-007")
    return request.principal


def _document_id(request: Request) -> int:
    try:
        return int(request.path_params["documentId"])
    except (KeyError, TypeError, ValueError):
        raise PublicError("COMMON-002") from None


def _json_object(request: Request) -> Mapping[str, object]:
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicError("COMMON-002") from None
    if not isinstance(value, dict):
        body_violation("body", "JSON 객체여야 합니다.")
    return value


def _multipart(request: Request) -> tuple[UploadFile | None, dict[str, str]]:
    content_type = request.header("content-type")
    if content_type is None or not content_type.lower().startswith("multipart/form-data"):
        raise PublicError("COMMON-005")
    if "\r" in content_type or "\n" in content_type:
        raise PublicError("COMMON-002")
    try:
        header = content_type.encode("ascii")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + header + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.body
        )
    except (UnicodeEncodeError, ValueError):
        raise PublicError("COMMON-002") from None
    if (
        message.get_content_type() != "multipart/form-data"
        or not message.is_multipart()
        or not message.get_boundary()
    ):
        raise PublicError("COMMON-002")

    upload: UploadFile | None = None
    fields: dict[str, str] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            raise PublicError("COMMON-002")
        data = part.get_payload(decode=True)
        if data is None:
            raise PublicError("COMMON-002")
        if name == "file":
            upload = UploadFile(data, part.get_filename(), part.get("Content-Type"))
            continue
        try:
            fields[name] = data.decode(part.get_content_charset() or "utf-8")
        except (LookupError, UnicodeDecodeError):
            raise PublicError("COMMON-002") from None
    return upload, fields


class DocumentApi:
    """Thin HTTP projection over the document feature's public methods."""

    def __init__(self, workspace: DocumentWorkspace) -> None:
        self.workspace = workspace

    def mount(self, app: object) -> None:
        add_route = getattr(app, "add_route")
        add_route("POST", "/api/documents", self.upload, success_status=201)
        add_route(
            "POST",
            "/api/documents/{documentId}/versions",
            self.add_version,
            success_status=201,
        )
        add_route("GET", "/api/documents", self.list)
        add_route("GET", "/api/documents/{documentId}", self.detail)
        add_route("GET", "/api/documents/{documentId}/content", self.content)
        add_route("GET", "/api/documents/{documentId}/file", self.file)
        add_route("GET", "/api/documents/{documentId}/status", self.status)
        add_route(
            "PATCH", "/api/documents/{documentId}", self.update_metadata, success_status=204
        )
        add_route(
            "PATCH",
            "/api/documents/{documentId}/visibility",
            self.update_visibility,
            success_status=204,
        )
        add_route("DELETE", "/api/documents/{documentId}", self.delete, success_status=204)

    def upload(self, request: Request) -> dict[str, object]:
        upload, fields = _multipart(request)
        if upload is None:
            raise PublicError("DOCUMENT-FILE-001")
        title = fields.get("title")
        visibility = fields.get("visibility")
        if title is None or visibility is None:
            raise PublicError("COMMON-002")
        return self.workspace.upload(
            _principal(request),
            upload,
            title=title,
            description=fields.get("description"),
            visibility=visibility,
        ).data

    def add_version(self, request: Request) -> dict[str, object]:
        upload, _ = _multipart(request)
        if upload is None:
            raise PublicError("DOCUMENT-FILE-001")
        return self.workspace.add_version(
            _principal(request), _document_id(request), upload
        ).data

    def list(self, request: Request) -> dict[str, object]:
        query = request.query_params
        status = query.get("status")
        if status is not None and status not in DOCUMENT_STATUSES:
            raise PublicError("COMMON-002")
        try:
            page = int(query.get("page", "0"))
            size = int(query.get("size", "20"))
        except (TypeError, ValueError):
            raise PublicError("COMMON-002") from None
        return self.workspace.list(
            _principal(request), status=status, page=page, size=size
        )

    def detail(self, request: Request) -> dict[str, object]:
        return self.workspace.detail(_principal(request), _document_id(request))

    def content(self, request: Request) -> dict[str, object]:
        return self.workspace.content(_principal(request), _document_id(request))

    def file(self, request: Request):
        return self.workspace.file(
            _principal(request),
            _document_id(request),
            request.query_params.get("disposition"),
        )

    def status(self, request: Request) -> dict[str, object]:
        return self.workspace.status(_principal(request), _document_id(request))

    def update_metadata(self, request: Request) -> None:
        body = _json_object(request)
        title = body.get("title")
        description = body.get("description")
        if not isinstance(title, str) or not title.strip():
            body_violation("title", "필수 문자열이며 공백일 수 없습니다.")
        if len(title.strip()) > 500:
            body_violation("title", "최대 500자이어야 합니다.")
        if "description" not in body:
            body_violation("description", "필수 필드이며 null을 허용합니다.")
        if description is not None and not isinstance(description, str):
            body_violation("description", "문자열 또는 null이어야 합니다.")
        self.workspace.update_metadata(
            _principal(request),
            _document_id(request),
            title=title,
            description=description,
        )

    def update_visibility(self, request: Request) -> None:
        visibility = _json_object(request).get("visibility")
        if not isinstance(visibility, str):
            body_violation("visibility", "필수 문자열이어야 합니다.")
        self.workspace.update_visibility(
            _principal(request), _document_id(request), visibility
        )

    def delete(self, request: Request) -> None:
        self.workspace.delete(_principal(request), _document_id(request))


def register_document_routes(app: object, workspace: DocumentWorkspace) -> DocumentApi:
    api = DocumentApi(workspace)
    api.mount(app)
    return api
