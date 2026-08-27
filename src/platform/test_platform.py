from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.infra.http.testing import request_over_uvicorn
from src.platform import (
    ALLOWED_CORS_ORIGINS,
    AccessDeniedError,
    AuthenticationError,
    BodyValidationError,
    DataAccessError,
    IntegrityConstraintError,
    MAX_REQUEST_BYTES,
    PlatformApp,
    RequestValidationError,
    UnsupportedMediaTypeError,
    UploadTooLargeError,
    origin_allowed,
    page_payload,
)
from src.shared import Principal, Request


FIXED_NOW = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)


def _resolver(request):
    token = request.header("authorization")
    if token == "Bearer user-token":
        return Principal("user@example.com", frozenset({"USER"}))
    if token == "Bearer admin-token":
        return Principal("admin@example.com", frozenset({"USER", "ADMIN"}))
    return None


def _call(app, method, path, headers=None, body=b""):
    response = request_over_uvicorn(
        app.handle, Request(method, path, headers or {}, body)
    )
    return response.status, dict(response.headers), response.body


class PlatformAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.app = PlatformApp(_resolver, lambda: FIXED_NOW)
        self.app.add_route("GET", "/documents", lambda request: [])
        self.app.add_route("GET", "/admin/users", lambda request: [])
        self.app.add_route("GET", "/test/empty", lambda request: None)
        self.app.add_route("GET", "/test/no-content", lambda request: None, success_status=204)
        self.app.add_route("POST", "/test/created", lambda request: {"id": 1}, success_status=201)

        def invalid_body(request):
            raise BodyValidationError(
                (("email", "이메일 형식이 아닙니다."), ("password", "비밀번호가 너무 짧습니다."))
            )

        self.app.add_route("POST", "/test/validate", invalid_body)

    def test_AC_SYS_001_protected_path_has_401_body(self):
        status, _, raw = _call(self.app, "GET", "/documents?ignored=true")
        body = json.loads(raw)

        self.assertEqual(401, status)
        self.assertEqual(
            {"success", "status", "code", "message", "method", "path", "timestamp"},
            set(body),
        )
        self.assertEqual("COMMON-007", body["code"])
        self.assertEqual("인증이 필요합니다.", body["message"])
        self.assertEqual("GET", body["method"])
        self.assertEqual("/documents", body["path"])
        self.assertEqual("2026-08-27 00:30:00", body["timestamp"])

    def test_AC_SYS_002_user_cannot_access_admin_path(self):
        status, _, raw = _call(
            self.app,
            "GET",
            "/admin/users",
            {"Authorization": "Bearer user-token"},
        )
        body = json.loads(raw)

        self.assertEqual(403, status)
        self.assertEqual("ROLE-002", body["code"])
        self.assertEqual("접근 권한이 없습니다.", body["message"])

    def test_AC_SYS_005_cors_preflight_policy(self):
        status, headers, raw = _call(
            self.app,
            "OPTIONS",
            "/documents",
            {
                "Origin": "https://not-allowed.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(b"", raw)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, headers, raw = _call(
            self.app,
            "OPTIONS",
            "/documents",
            {
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(204, status)
        self.assertEqual(b"", raw)
        self.assertEqual("http://localhost:3000", headers["Access-Control-Allow-Origin"])
        self.assertEqual("true", headers["Access-Control-Allow-Credentials"])
        self.assertEqual("3600", headers["Access-Control-Max-Age"])
        self.assertEqual("GET,POST,PUT,DELETE,OPTIONS,PATCH", headers["Access-Control-Allow-Methods"])
        self.assertTrue(all(origin_allowed(origin) for origin in ALLOWED_CORS_ORIGINS))
        self.assertFalse(origin_allowed("https://not-allowed.example"))
        with patch.dict(
            "os.environ",
            {"CORS_ALLOWED_ORIGINS": "https://vectorshelf.example.com, http://10.0.0.5:5173"},
        ):
            self.assertTrue(origin_allowed("https://vectorshelf.example.com"))
            self.assertTrue(origin_allowed("http://10.0.0.5:5173"))
            self.assertFalse(origin_allowed("https://not-allowed.example"))

    def test_AC_SYS_006_method_and_framework_error_mapping(self):
        status, _, raw = _call(self.app, "DELETE", "/test/empty")
        body = json.loads(raw)
        self.assertEqual(405, status)
        self.assertEqual("COMMON-004", body["code"])

        status, _, raw = _call(
            self.app,
            "POST",
            "/test/created",
            {"Content-Type": "text/plain"},
            b'{"id":1}',
        )
        body = json.loads(raw)
        self.assertEqual((415, "COMMON-005"), (status, body["code"]))

        status, _, raw = _call(
            self.app,
            "POST",
            "/test/created",
            {"Content-Type": "application/json; charset=utf-8"},
            b'{"id":1}',
        )
        self.assertEqual(201, status)

        cases = (
            (RequestValidationError(), 400, "COMMON-002", "요청 파라미터가 올바르지 않습니다."),
            (UnsupportedMediaTypeError(), 415, "COMMON-005", "지원하지 않는 미디어 타입입니다."),
            (UploadTooLargeError(), 400, "DOCUMENT-FILE-002", "파일 용량이 큽니다."),
            (AuthenticationError(), 401, "COMMON-007", "인증이 필요합니다."),
            (IntegrityConstraintError(), 409, "COMMON-008", "데이터 무결성 오류가 발생했습니다."),
            (DataAccessError(), 500, "COMMON-006", "서버 내부 오류가 발생했습니다."),
            (AccessDeniedError(), 403, "ROLE-002", "접근 권한이 없습니다."),
            (RuntimeError("must not leak"), 500, "COMMON-006", "서버 내부 오류가 발생했습니다."),
        )
        for index, (error, expected_status, expected_code, expected_message) in enumerate(cases):
            path = f"/test/error-{index}"

            def fail(request, captured=error):
                raise captured

            self.app.add_route("GET", path, fail)
            with self.subTest(code=expected_code):
                status, _, raw = _call(self.app, "GET", path)
                body = json.loads(raw)
                self.assertEqual(expected_status, status)
                self.assertEqual(status, body["status"])
                self.assertEqual(expected_code, body["code"])
                self.assertEqual(expected_message, body["message"])
                self.assertNotIn("must not leak", body["message"])

    def test_AC_SYS_007_only_first_body_violation_is_exposed(self):
        status, _, raw = _call(self.app, "POST", "/test/validate")
        body = json.loads(raw)

        self.assertEqual(400, status)
        self.assertEqual("COMMON-002", body["code"])
        self.assertEqual("email: 이메일 형식이 아닙니다.", body["message"])
        self.assertNotIn("password", body["message"])

    def test_AC_SYS_007_query_string_reaches_route_without_changing_path(self):
        observed = {}

        def handler(request):
            observed.update(request.query_params)
            return {"ok": True}

        self.app.add_route("GET", "/test/query", handler)
        status, _, _ = _call(
            self.app, "GET", "/test/query?page=2&status=FAILED"
        )
        self.assertEqual(200, status)
        self.assertEqual({"page": "2", "status": "FAILED"}, observed)

    def test_AC_SYS_008_empty_success_omits_data(self):
        status, _, raw = _call(self.app, "GET", "/test/empty")
        body = json.loads(raw)
        self.assertEqual(200, status)
        self.assertEqual({"success", "status", "timestamp"}, set(body))
        self.assertEqual("2026-08-27 00:30:00", body["timestamp"])

        for index, value in enumerate((False, 0, "", [], {})):
            path = f"/test/value-{index}"
            self.app.add_route("GET", path, lambda request, captured=value: captured)
            status, _, raw = _call(self.app, "GET", path)
            self.assertEqual(200, status)
            self.assertIn("data", json.loads(raw))
            self.assertEqual(value, json.loads(raw)["data"])

        page = page_payload(
            content=[{"id": 1}],
            page=0,
            size=20,
            total_elements=1,
            total_pages=1,
            first=True,
            last=True,
        )
        self.assertEqual(
            {"content", "page", "size", "totalElements", "totalPages", "first", "last"},
            set(page),
        )

        status, _, raw = _call(self.app, "POST", "/test/created")
        self.assertEqual(201, status)
        self.assertEqual(201, json.loads(raw)["status"])

        status, headers, raw = _call(self.app, "GET", "/test/no-content")
        self.assertEqual(204, status)
        self.assertEqual({}, headers)
        self.assertEqual(b"", raw)

        self.app.add_route(
            "POST",
            "/test/dynamic-status",
            lambda request: SimpleNamespace(
                status=201, data={"id": 2, "createdAt": FIXED_NOW}
            ),
        )
        status, _, raw = _call(self.app, "POST", "/test/dynamic-status")
        self.assertEqual(201, status)
        self.assertEqual(
            {"id": 2, "createdAt": "2026-08-26T15:30:00+00:00"},
            json.loads(raw)["data"],
        )
        self.assertEqual("2026-08-27 00:30:00", json.loads(raw)["timestamp"])

    def test_AC_DOC_003_transport_rejects_request_over_60mb_before_read(self):
        class MustNotRead:
            def read(self, size=-1):
                raise AssertionError("oversized request body was read")

        captured = {}
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/documents",
            "CONTENT_LENGTH": str(MAX_REQUEST_BYTES + 1),
            "wsgi.input": MustNotRead(),
        }

        raw = b"".join(
            self.app(
                environ,
                lambda status, headers: captured.update(
                    status=int(status.split()[0]), headers=dict(headers)
                ),
            )
        )

        self.assertEqual(400, captured["status"])
        self.assertEqual("DOCUMENT-FILE-002", json.loads(raw)["code"])


if __name__ == "__main__":
    unittest.main()
