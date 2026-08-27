"""REST registration for departments, roles, and administrator user changes."""

from __future__ import annotations

import json
from collections.abc import Mapping

from src.shared import PublicError, Request, body_violation


def _json_object(request: Request) -> Mapping[str, object]:
    try:
        value = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicError("COMMON-002") from error
    if not isinstance(value, Mapping):
        body_violation("body", "JSON 객체여야 합니다.")
    return value


def _user_id(request: Request) -> int:
    try:
        value = int(request.path_params["userId"])
    except (KeyError, TypeError, ValueError) as error:
        raise PublicError("COMMON-002") from error
    if value < 1:
        raise PublicError("COMMON-002")
    return value


def _authenticated(request: Request) -> None:
    if request.principal is None:
        raise PublicError("COMMON-007")


def _admin(request: Request) -> int:
    principal = request.principal
    if principal is None:
        raise PublicError("COMMON-007")
    if "ADMIN" not in principal.roles:
        raise PublicError("ROLE-002")
    if principal.user_id is None:
        raise PublicError("COMMON-007")
    return principal.user_id


def register_user_routes(app: object, directory: object) -> None:
    """Attach FR-AUTH-005~010 using the injected identity ledger."""

    def departments(request: Request) -> object:
        return directory.departments_data()

    def roles(request: Request) -> object:
        _authenticated(request)
        return directory.roles_data()

    def users(request: Request) -> object:
        _admin(request)
        return directory.users_data()

    def grant_role(request: Request) -> None:
        actor_id = _admin(request)
        data = _json_object(request)
        role_code = data.get("roleCode")
        if not isinstance(role_code, str) or not role_code.strip():
            body_violation("roleCode", "필수 문자열이며 공백일 수 없습니다.")
        directory.assign_role(
            _user_id(request), role_code, granted_by_user_id=actor_id
        )

    def revoke_role(request: Request) -> None:
        _admin(request)
        role_code = request.path_params.get("roleCode")
        if not isinstance(role_code, str) or not role_code.strip():
            raise PublicError("COMMON-002")
        directory.remove_role(_user_id(request), role_code)

    def change_department(request: Request) -> None:
        _admin(request)
        data = _json_object(request)
        department_id = data.get("departmentId")
        if (
            isinstance(department_id, bool)
            or not isinstance(department_id, int)
            or department_id < 1
        ):
            body_violation("departmentId", "1 이상의 정수여야 합니다.")
        directory.change_department(_user_id(request), department_id)

    add_route = getattr(app, "add_route")
    add_route("GET", "/departments", departments)
    add_route("GET", "/roles", roles)
    add_route("GET", "/admin/users", users)
    add_route(
        "POST",
        "/admin/users/{userId}/roles",
        grant_role,
        success_status=201,
    )
    add_route(
        "DELETE",
        "/admin/users/{userId}/roles/{roleCode}",
        revoke_role,
        success_status=204,
    )
    add_route(
        "PATCH",
        "/admin/users/{userId}/department",
        change_department,
        success_status=204,
    )
