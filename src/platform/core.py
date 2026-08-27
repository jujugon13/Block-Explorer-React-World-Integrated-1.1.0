"""Phase-1 platform behavior from VectorShelf §§6.1, 10.2, and FR-SYS-065."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from io import BytesIO
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from src.shared import (
    BodyValidationError,
    Handler,
    Principal,
    PrincipalResolver,
    PublicError,
    Request,
    Response,
)

SEOUL = ZoneInfo("Asia/Seoul")
ALLOWED_CORS_ORIGINS = frozenset(
    {
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
    }
)
ALLOWED_CORS_METHODS = ("GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH")
MAX_REQUEST_BYTES = 60 * 1024 * 1024

ERRORS: dict[str, tuple[int, str]] = {
    "COMMON-001": (400, "잘못된 요청입니다."),
    "COMMON-002": (400, "요청 파라미터가 올바르지 않습니다."),
    "COMMON-003": (404, "리소스를 찾을 수 없습니다."),
    "COMMON-004": (405, "지원하지 않는 HTTP 메서드입니다."),
    "COMMON-005": (415, "지원하지 않는 미디어 타입입니다."),
    "COMMON-006": (500, "서버 내부 오류가 발생했습니다."),
    "COMMON-007": (401, "인증이 필요합니다."),
    "COMMON-008": (409, "데이터 충돌이 발생했습니다."),
    "ROLE-002": (403, "접근 권한이 없습니다."),
    "COLLECTION-001": (404, "컬렉션을 찾을 수 없습니다."),
    "COLLECTION-002": (409, "이미 컬렉션에 추가된 문서입니다."),
    "COLLECTION-003": (404, "컬렉션에서 해당 문서를 찾을 수 없습니다."),
    "COLLECTION-004": (400, "공개 범위는 PRIVATE 또는 PUBLIC만 지정할 수 있습니다."),
    "DOCUMENT-001": (404, "문서를 찾을 수 없습니다."),
    "DOCUMENT-VERSION-001": (409, "현재 버전과 동일한 파일입니다."),
    "DOCUMENT-VERSION-002": (409, "처리 중인 문서 버전이 있습니다."),
    "DOCUMENT-VERSION-003": (409, "현재 문서 상태에서는 새 버전을 추가할 수 없습니다."),
    "DOCUMENT-VERSION-004": (400, "기존 문서와 다른 파일 형식은 업로드할 수 없습니다."),
    "DOCUMENT-VERSION-005": (409, "현재 문서 버전 상태에서는 Chunk를 생성할 수 없습니다."),
    "DOCUMENT-VERSION-006": (409, "현재 문서 버전 상태에서는 Embedding을 생성할 수 없습니다."),
    "DOCUMENT-FILE-001": (400, "빈 파일은 업로드할 수 없습니다."),
    "DOCUMENT-FILE-002": (400, "파일 용량이 큽니다."),
    "DOCUMENT-FILE-003": (400, "지원하지 않는 파일 확장자입니다."),
    "DOCUMENT-FILE-004": (400, "지원하지 않는 파일 형식입니다."),
    "DOCUMENT-FILE-005": (400, "유효하지 않은 파일명입니다."),
    "DOCUMENT-FILE-006": (500, "파일 해시를 계산하지 못했습니다."),
    "DOCUMENT-STORAGE-001": (503, "파일 저장소를 사용할 수 없습니다."),
    "DOCUMENT-STORAGE-002": (404, "저장된 파일을 찾을 수 없습니다."),
    "DOCUMENT-STORAGE-003": (500, "파일 저장소 설정이 저장된 파일 위치와 일치하지 않습니다."),
    "DOCUMENT-PARSING-001": (422, "지원하지 않는 문서 형식입니다."),
    "DOCUMENT-PARSING-002": (422, "문서에서 처리할 텍스트를 찾을 수 없습니다."),
    "DOCUMENT-PARSING-003": (422, "문서 텍스트를 UTF-8로 해석할 수 없습니다."),
    "DOCUMENT-PARSING-004": (500, "문서 원본 파일 정보를 확인할 수 없습니다."),
    "DOCUMENT-PARSING-005": (422, "암호화된 PDF 문서는 처리할 수 없습니다."),
    "DOCUMENT-PARSING-006": (422, "PDF에서 텍스트를 찾을 수 없어 OCR 처리가 필요합니다."),
    "DOCUMENT-PARSING-007": (422, "문서 내용을 읽을 수 없습니다."),
    "DOCUMENT-PARSING-008": (422, "문서 텍스트에 깨진 문자가 많아 처리할 수 없습니다."),
    "DOCUMENT-CONTENT-001": (409, "현재 문서 버전의 추출 본문을 아직 조회할 수 없습니다."),
    "DOCUMENT-STATUS-001": (500, "문서 인덱싱 상태를 조회할 수 없습니다."),
    "DOCUMENT-UPLOAD-001": (500, "파일 정보를 저장하지 못했습니다."),
    "DOCUMENT-CHUNK-001": (500, "문서 버전과 Chunk 데이터가 일치하지 않습니다."),
    "DOCUMENT-EMBEDDING-001": (500, "문서 버전과 Embedding 데이터가 일치하지 않습니다."),
    "DOCUMENT-EMBEDDING-002": (500, "생성된 Embedding Vector가 올바르지 않습니다."),
    "DOCUMENT-INDEXING-001": (409, "현재 상태에서는 문서 인덱싱을 완료할 수 없습니다."),
    "DOCUMENT-INDEXING-002": (409, "최신 문서 버전이 아니므로 인덱싱을 완료할 수 없습니다."),
    "DOCUMENT-INDEXING-003": (500, "문서 인덱싱 완료 데이터를 확인할 수 없습니다."),
    "DOCUMENT-INDEXING-004": (500, "문서 인덱싱 실패 데이터를 확인할 수 없습니다."),
    "DOCUMENT-VISIBILITY-001": (400, "공개 범위는 PRIVATE 또는 PUBLIC만 지정할 수 있습니다."),
    "USER-001": (404, "사용자를 찾을 수 없습니다."),
    "USER-002": (409, "이미 사용 중인 이메일입니다."),
    "USER-003": (403, "비활성화된 계정입니다."),
    "USER-004": (401, "이메일 또는 비밀번호가 올바르지 않습니다."),
    "USER-005": (400, "비밀번호는 이메일 또는 이름과 같을 수 없습니다."),
    "DEPT-001": (400, "존재하지 않는 부서입니다."),
    "ROLE-001": (400, "존재하지 않는 역할입니다."),
    "ROLE-003": (409, "이미 부여된 역할입니다."),
    "ROLE-004": (404, "부여되지 않은 역할입니다."),
    "PERMISSION-001": (400, "target_type과 ID 필드 조합이 올바르지 않습니다."),
    "PERMISSION-002": (404, "컬렉션 권한을 찾을 수 없습니다."),
    "PERMISSION-003": (404, "문서 권한을 찾을 수 없습니다."),
    "PERMISSION-004": (
        400,
        "USER role은 모든 사용자가 보유하고 있어 권한 부여 대상으로 지정할 수 없습니다. 전체 공개가 목적이면 visibility를 PUBLIC으로 설정하세요.",
    ),
    "WORKER-001": (404, "Worker를 찾을 수 없습니다."),
    "WORKER-002": (409, "Worker가 Job을 처리할 수 없는 상태입니다."),
    "EMBEDDING-JOB-001": (404, "Embedding Job을 찾을 수 없습니다."),
    "EMBEDDING-JOB-002": (409, "현재 상태에서는 Embedding Job Attempt를 시작할 수 없습니다."),
    "EMBEDDING-JOB-003": (409, "현재 Embedding Job 소유권과 요청이 일치하지 않습니다."),
    "EMBEDDING-JOB-004": (409, "Embedding Job Lease가 만료되었습니다."),
    "EMBEDDING-JOB-005": (500, "Embedding Job 소유권 데이터를 확인할 수 없습니다."),
    "EMBEDDING-JOB-006": (409, "현재 Claim 실행 Context와 Attempt가 일치하지 않습니다."),
    "EMBEDDING-JOB-007": (409, "동일한 Embedding Job Attempt에 다른 실패 내용이 이미 기록되었습니다."),
    "EMBEDDING-JOB-008": (409, "최종 실패한 Embedding Job만 수동으로 재처리할 수 있습니다."),
    "EMBEDDING-JOB-009": (409, "현재 문서 상태에서는 Embedding Job을 수동으로 재처리할 수 없습니다."),
    "EMBEDDING-MODEL-001": (500, "사용 가능한 임베딩 모델이 설정되지 않았습니다."),
    "EMBEDDING-MODEL-002": (500, "사용 가능한 임베딩 모델이 여러 개 설정되어 있습니다."),
    "EMBEDDING_SERVICE_ERROR": (503, "임베딩 서비스를 사용할 수 없습니다."),
    "CIRCUIT_BREAKER_OPEN": (503, "임베딩 서비스 회로 차단기가 열려 있습니다."),
    "GUARDRAIL_VIOLATION": (400, "이 질문은 처리할 수 없습니다."),
    "SEARCH_SERVICE_ERROR": (503, "검색 서비스를 사용할 수 없습니다."),
    "SEARCH_RATE_LIMIT": (429, "검색 요청 한도를 초과했습니다."),
    "MCP-001": (429, "도구 호출 한도를 초과했습니다."),
    "SYNC-001": (404, "동기화 Event를 찾을 수 없습니다."),
    "SYNC-002": (409, "현재 동기화 Event 소유권과 요청이 일치하지 않습니다."),
    "SYNC-003": (500, "동기화 Event와 도메인 상태가 일치하지 않습니다."),
    "SYNC-004": (409, "최종 실패한 동기화 Event만 재처리할 수 있습니다."),
    "SYNC-005": (404, "동기화 정합성 Issue를 찾을 수 없습니다."),
    "SYNC-006": (409, "현재 Issue는 안전한 자동 복구를 요청할 수 없습니다."),
    "SYNC-007": (409, "OPEN 상태의 Issue만 무시할 수 있습니다."),
}


class PlatformError(Exception):
    """A framework-level failure already classified by the public error ledger."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if code not in ERRORS:
            raise ValueError(f"Unknown public error code: {code}")
        super().__init__(message or ERRORS[code][1])
        self.code = code
        self.message = message


class RequestValidationError(Exception):
    """Path, query, type, or body parsing validation failure."""


class MethodNotAllowedError(Exception):
    pass


class UnsupportedMediaTypeError(Exception):
    pass


class UploadTooLargeError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class AccessDeniedError(Exception):
    pass


class IntegrityConstraintError(Exception):
    pass


class DataAccessError(Exception):
    pass


EXCEPTION_MAP: tuple[tuple[type[Exception], str, str | None], ...] = (
    (RequestValidationError, "COMMON-002", None),
    (MethodNotAllowedError, "COMMON-004", None),
    (UnsupportedMediaTypeError, "COMMON-005", None),
    (UploadTooLargeError, "DOCUMENT-FILE-002", None),
    (AuthenticationError, "COMMON-007", None),
    (AccessDeniedError, "ROLE-002", None),
    (IntegrityConstraintError, "COMMON-008", "데이터 무결성 오류가 발생했습니다."),
    (DataAccessError, "COMMON-006", None),
)


def _timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(SEOUL)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SEOUL)
    return current.astimezone(SEOUL).strftime("%Y-%m-%d %H:%M:%S")


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_response(status: int, payload: dict[str, object]) -> Response:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return Response(
        status,
        body,
        (("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))),
    )


def success_response(
    status: int = 200,
    data: object | None = None,
    *,
    now: datetime | None = None,
) -> Response:
    if status == 204:
        return Response(204)
    payload: dict[str, object] = {
        "success": True,
        "status": status,
        "timestamp": _timestamp(now),
    }
    if data is not None:
        payload["data"] = data
    return _json_response(status, payload)


def page_payload(
    *,
    content: Iterable[object],
    page: int,
    size: int,
    total_elements: int,
    total_pages: int,
    first: bool,
    last: bool,
) -> dict[str, object]:
    return {
        "content": list(content),
        "page": page,
        "size": size,
        "totalElements": total_elements,
        "totalPages": total_pages,
        "first": first,
        "last": last,
    }


def error_response(
    code: str,
    request: Request,
    message: str | None = None,
    *,
    now: datetime | None = None,
) -> Response:
    status, fixed_message = ERRORS[code]
    return _json_response(
        status,
        {
            "success": False,
            "status": status,
            "code": code,
            "message": message or fixed_message,
            "method": request.method,
            "path": request.path,
            "timestamp": _timestamp(now),
        },
    )


def _mapped_error(error: Exception, request: Request, now: datetime) -> Response:
    if isinstance(error, BodyValidationError):
        field, detail = error.violations[0]
        return error_response("COMMON-002", request, f"{field}: {detail}", now=now)
    if isinstance(error, PlatformError):
        return error_response(error.code, request, error.message, now=now)
    if isinstance(error, PublicError):
        return error_response(error.code, request, error.message, now=now)
    for error_type, code, message in EXCEPTION_MAP:
        if isinstance(error, error_type):
            return error_response(code, request, message, now=now)
    return error_response("COMMON-006", request, now=now)


def _is_public(path: str) -> bool:
    if path in {"/departments", "/auth/signup", "/auth/login", "/auth/logout"}:
        return True
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in ("/test", "/swagger-ui", "/v3/api-docs", "/ws")
    )


def _is_admin(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def origin_allowed(origin: str | None) -> bool:
    configured = {
        value.strip()
        for value in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    return origin in ALLOWED_CORS_ORIGINS or origin in configured


def _cors_headers(origin: str, *, preflight: bool = False) -> tuple[tuple[str, str], ...]:
    headers = (
        ("Access-Control-Allow-Origin", origin),
        ("Access-Control-Allow-Credentials", "true"),
    )
    if not preflight:
        return headers
    return headers + (
        ("Access-Control-Allow-Methods", ",".join(ALLOWED_CORS_METHODS)),
        ("Access-Control-Max-Age", "3600"),
    )


def _with_headers(response: Response, headers: tuple[tuple[str, str], ...]) -> Response:
    return Response(response.status, response.body, response.headers + headers)


class PlatformApp:
    """Small WSGI-compatible platform shell; domain routes are supplied later."""

    def __init__(
        self,
        principal_resolver: PrincipalResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        state_transition_committed: Callable[[], None] | None = None,
    ) -> None:
        self._principal_resolver = principal_resolver or (lambda request: None)
        self._clock = clock or (lambda: datetime.now(SEOUL))
        self._state_transition_committed = state_transition_committed
        self._routes: dict[str, dict[str, tuple[Handler, int]]] = {}

    def add_route(
        self,
        method: str,
        path: str,
        handler: Handler,
        *,
        success_status: int = 200,
    ) -> None:
        self._routes.setdefault(path, {})[method.upper()] = (handler, success_status)

    def handle(self, request: Request) -> Response:
        origin = request.header("origin")
        requested_method = request.header("access-control-request-method")
        if request.method == "OPTIONS" and origin and requested_method:
            if not origin_allowed(origin) or requested_method.upper() not in ALLOWED_CORS_METHODS:
                return Response(204)
            return Response(204, headers=_cors_headers(origin, preflight=True))

        now = self._clock()
        try:
            declared_length = request.header("content-length")
            if declared_length is not None:
                try:
                    parsed_length = int(declared_length)
                except ValueError:
                    raise RequestValidationError from None
                if parsed_length < 0:
                    raise RequestValidationError
                if parsed_length > MAX_REQUEST_BYTES:
                    raise UploadTooLargeError
            if len(request.body) > MAX_REQUEST_BYTES:
                raise UploadTooLargeError
            principal = self._principal_resolver(request)
            self._authorize(request.path, principal)
            methods, path_params = self._match_route(request.path)
            if methods is None:
                raise PlatformError("COMMON-003")
            route = methods.get(request.method)
            if route is None:
                raise MethodNotAllowedError
            self._validate_media_type(request)
            handler, status = route
            result = handler(replace(request, principal=principal, path_params=path_params))
            if isinstance(result, Response):
                response = result
            elif hasattr(result, "status") and hasattr(result, "data"):
                result_status = getattr(result, "status")
                if (
                    isinstance(result_status, bool)
                    or result_status not in {200, 201, 204}
                ):
                    raise TypeError("operation result has an invalid HTTP status")
                response = success_response(
                    result_status, getattr(result, "data"), now=now
                )
            else:
                response = success_response(status, result, now=now)
            if (
                self._state_transition_committed is not None
                and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and 200 <= response.status < 300
            ):
                try:
                    self._state_transition_committed()
                except Exception:
                    pass
        except Exception as error:
            response = _mapped_error(error, request, now)

        if origin_allowed(origin):
            response = _with_headers(response, _cors_headers(origin))
        return response

    @staticmethod
    def _validate_media_type(request: Request) -> None:
        """Reject a body before feature JSON/multipart decoding sees it."""

        if not request.body or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return
        media_type = (request.header("content-type") or "").split(";", 1)[0].strip().lower()
        segments = request.path.strip("/").split("/")
        multipart_route = request.path == "/api/documents" or (
            len(segments) == 4
            and segments[:2] == ["api", "documents"]
            and segments[3] == "versions"
        )
        expected = "multipart/form-data" if multipart_route else "application/json"
        if media_type != expected:
            raise UnsupportedMediaTypeError

    def _match_route(self, path: str) -> tuple[dict[str, tuple[Handler, int]] | None, dict[str, str]]:
        exact = self._routes.get(path)
        if exact is not None:
            return exact, {}
        actual = path.strip("/").split("/") if path != "/" else []
        for template, methods in self._routes.items():
            expected = template.strip("/").split("/") if template != "/" else []
            if len(actual) != len(expected):
                continue
            params: dict[str, str] = {}
            for wanted, value in zip(expected, actual):
                if wanted.startswith("{") and wanted.endswith("}") and len(wanted) > 2:
                    params[wanted[1:-1]] = value
                elif wanted != value:
                    break
            else:
                return methods, params
        return None, {}

    @staticmethod
    def _authorize(path: str, principal: Principal | None) -> None:
        # The exact protocol endpoint authenticates its long-lived API key in
        # the MCP handler. Token-management descendants remain protected by JWT.
        if path == "/mcp":
            return
        if _is_public(path):
            return
        if principal is None:
            raise AuthenticationError
        if _is_admin(path) and "ADMIN" not in principal.roles:
            raise AccessDeniedError

    def __call__(self, environ: dict[str, object], start_response: Callable) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = str(environ.get("PATH_INFO", "/"))
        query = str(environ.get("QUERY_STRING", ""))
        headers: dict[str, str] = {}
        for name, value in environ.items():
            if name.startswith("HTTP_"):
                headers[name[5:].replace("_", "-").lower()] = str(value)
        for name in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if name in environ and environ[name] not in (None, ""):
                headers[name.replace("_", "-").lower()] = str(environ[name])

        try:
            length = max(0, int(headers.get("content-length", "0")))
        except ValueError:
            length = 0
        stream = environ.get("wsgi.input") or BytesIO()
        body = stream.read(length) if 0 < length <= MAX_REQUEST_BYTES else b""
        target = f"{path}?{query}" if query else path
        response = self.handle(Request(method, target, headers, body))
        start_response(f"{response.status} {HTTPStatus(response.status).phrase}", list(response.headers))
        return [response.body]
