from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from src.platform import PlatformApp
from src.shared import Principal, Request
from src.users import UserDirectory, register_user_routes


NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _body(response):
    return json.loads(response.body) if response.body else None


class UsersAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = UserDirectory()
        self.directory.seed_department(1, "Engineering")
        self.directory.seed_department(2, "Operations")
        self.directory.seed_role("USER")
        self.directory.seed_role("ADMIN")
        self.admin = self.directory.create_user(
            "admin@example.com", "hash", "Admin", 1, NOW
        )
        self.directory.assign_role(
            self.admin.id, "ADMIN", granted_by_user_id=self.admin.id, now=NOW
        )
        self.user = self.directory.create_user(
            "user@example.com", "hash", "User", 1, NOW
        )

        def resolver(request):
            return {
                "Bearer admin": Principal(
                    self.admin.email,
                    frozenset({"USER", "ADMIN"}),
                    self.admin.id,
                ),
                "Bearer user": Principal(
                    self.user.email, frozenset({"USER"}), self.user.id
                ),
            }.get(request.header("authorization"))

        self.app = PlatformApp(resolver, lambda: NOW)
        register_user_routes(self.app, self.directory)

    def test_AC_SYS_003_departments_endpoint_is_public(self):
        response = self.app.handle(Request("GET", "/departments"))
        body = _body(response)

        self.assertEqual(200, response.status)
        self.assertTrue(body["success"])
        self.assertEqual(200, body["status"])
        self.assertEqual(
            [
                {"departmentId": 1, "name": "Engineering", "status": "ACTIVE"},
                {"departmentId": 2, "name": "Operations", "status": "ACTIVE"},
            ],
            body["data"],
        )

    def test_AC_AUTH_013_roles_require_authentication_and_admin_routes_require_ADMIN(self):
        unauthenticated = self.app.handle(Request("GET", "/roles"))
        roles = self.app.handle(
            Request("GET", "/roles", {"Authorization": "Bearer user"})
        )
        forbidden = self.app.handle(
            Request("GET", "/admin/users", {"Authorization": "Bearer user"})
        )
        users = self.app.handle(
            Request("GET", "/admin/users", {"Authorization": "Bearer admin"})
        )

        self.assertEqual((401, "COMMON-007"),
                         (unauthenticated.status, _body(unauthenticated)["code"]))
        self.assertEqual(200, roles.status)
        self.assertEqual(["ADMIN", "USER"], [item["code"] for item in _body(roles)["data"]])
        self.assertEqual((403, "ROLE-002"), (forbidden.status, _body(forbidden)["code"]))
        self.assertEqual(2, len(_body(users)["data"]))

    def test_AC_AUTH_013_admin_role_and_department_routes_apply_and_validate_inputs(self):
        headers = {
            "Authorization": "Bearer admin",
            "Content-Type": "application/json",
        }
        grant = self.app.handle(
            Request(
                "POST",
                f"/admin/users/{self.user.id}/roles",
                headers,
                json.dumps({"roleCode": "ADMIN"}).encode(),
            )
        )
        assignment = self.directory.role_assignment(self.user.id, "ADMIN")
        self.assertEqual(201, grant.status)
        self.assertNotIn("data", _body(grant))
        self.assertEqual(self.admin.id, assignment.granted_by_user_id)

        revoke = self.app.handle(
            Request(
                "DELETE",
                f"/admin/users/{self.user.id}/roles/ADMIN",
                headers,
            )
        )
        change = self.app.handle(
            Request(
                "PATCH",
                f"/admin/users/{self.user.id}/department",
                headers,
                json.dumps({"departmentId": 2}).encode(),
            )
        )
        self.assertEqual((204, b""), (revoke.status, revoke.body))
        self.assertEqual((204, b""), (change.status, change.body))
        self.assertNotIn("ADMIN", self.directory.roles_for(self.user.id))
        self.assertEqual(2, self.directory.get_user(self.user.id).department_id)

        for path, raw in (
            ("/admin/users/not-an-id/roles", json.dumps({"roleCode": "ADMIN"}).encode()),
            (f"/admin/users/{self.user.id}/roles", b"{"),
            (
                f"/admin/users/{self.user.id}/department",
                json.dumps({"departmentId": True}).encode(),
            ),
        ):
            with self.subTest(path=path):
                response = self.app.handle(Request("POST" if path.endswith("roles") else "PATCH", path, headers, raw))
                self.assertEqual((400, "COMMON-002"), (response.status, _body(response)["code"]))

    def test_AC_SYS_007_user_body_violation_names_the_field(self):
        response = self.app.handle(
            Request(
                "PATCH",
                f"/admin/users/{self.user.id}/department",
                {
                    "Authorization": "Bearer admin",
                    "Content-Type": "application/json",
                },
                b'{"departmentId":true}',
            )
        )

        body = _body(response)
        self.assertEqual((400, "COMMON-002"), (response.status, body["code"]))
        self.assertEqual("departmentId: 1 이상의 정수여야 합니다.", body["message"])


if __name__ == "__main__":
    unittest.main()
