"""REST registration for authentication and token operations."""

from __future__ import annotations

import json
from collections.abc import Mapping

from src.shared import PublicError, Request, body_violation

from .core import EMAIL


def _json_object(request: Request) -> Mapping[str, object]:
    try:
        value = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicError("COMMON-002") from error
    if not isinstance(value, Mapping):
        body_violation("body", "JSON 객체여야 합니다.")
    return value


def _required_text(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        body_violation(field, "필수 문자열이며 공백일 수 없습니다.")
    return value


def register_auth_routes(app: object, auth: object) -> None:
    """Attach FR-AUTH-001~004 without coupling to a web framework."""

    def signup(request: Request) -> object:
        data = _json_object(request)
        email = _required_text(data, "email")
        if EMAIL.fullmatch(email) is None:
            body_violation("email", "이메일 형식이어야 합니다.")
        password = _required_text(data, "password")
        if not 12 <= len(password) <= 64:
            body_violation("password", "12자 이상 64자 이하이어야 합니다.")
        name = _required_text(data, "name")
        department_id = data.get("departmentId")
        if isinstance(department_id, bool) or not isinstance(department_id, int):
            body_violation("departmentId", "필수 정수여야 합니다.")
        known = {"email", "password", "name", "departmentId"}
        return auth.signup(
            email,
            password,
            name,
            department_id,
            **{key: value for key, value in data.items() if key not in known},
        )

    def login(request: Request) -> object:
        data = _json_object(request)
        return auth.login(
            _required_text(data, "email"),
            _required_text(data, "password"),
        )

    def me(request: Request) -> object:
        principal = request.principal
        if principal is None or principal.user_id is None:
            raise PublicError("COMMON-007")
        return auth.me(principal.user_id)

    def logout(request: Request) -> None:
        auth.logout(request.header("authorization"))

    add_route = getattr(app, "add_route")
    add_route("POST", "/auth/signup", signup, success_status=201)
    add_route("POST", "/auth/login", login)
    add_route("GET", "/auth/me", me)
    add_route("POST", "/auth/logout", logout, success_status=204)
