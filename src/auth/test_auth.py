from __future__ import annotations

import base64
import json
import unittest
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

from src.auth import AuthService, InMemoryCache, TokenManager, register_auth_routes
from src.infra.http.testing import request_over_uvicorn
from src.platform import PlatformApp
from src.shared import PublicError, Request
from src.users import UserDirectory


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def call(app: PlatformApp, method: str, path: str, *, token: str | None = None, data=None):
    body = b"" if data is None else json.dumps(data).encode()
    headers = {}
    if body:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    response = request_over_uvicorn(app.handle, Request(method, path, headers, body))
    raw = response.body
    payload = json.loads(raw) if raw else None
    return response.status, payload, raw


class AuthAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock(datetime(2026, 8, 27, 1, 0, 0, 250_000, tzinfo=UTC))
        self.directory = UserDirectory()
        self.directory.seed_department(1, "Engineering")
        self.directory.seed_department(2, "Disabled", "INACTIVE")
        self.directory.seed_role("USER")
        self.directory.seed_role("ADMIN")
        self.cache = InMemoryCache(self.clock)
        self.tokens = TokenManager("stage-two-test-secret")
        self.auth = AuthService(self.directory, self.cache, self.tokens, self.clock)
        self.app = self._app(self.auth)

    @staticmethod
    def _app(auth: AuthService) -> PlatformApp:
        app = PlatformApp(auth.resolve_request, auth.clock)
        register_auth_routes(app, auth)
        app.add_route("GET", "/protected", lambda request: {"ok": True})
        app.add_route("GET", "/admin/probe", lambda request: {"ok": True})
        return app

    def _signup_data(self, email: str, password: str = "CorrectPass!1", **overrides):
        data = {
            "email": email,
            "password": password,
            "name": "Test User",
            "departmentId": 1,
        }
        data.update(overrides)
        return data

    def _signup(self, email: str, password: str = "CorrectPass!1"):
        result = self.auth.signup(email, password, "Test User", 1)
        return self.directory.get_user(result["userId"])

    def _login_token(self, email: str, password: str = "CorrectPass!1") -> str:
        return self.auth.login(email, password)["accessToken"]

    def test_AC_AUTH_001_valid_signup_assigns_USER(self):
        status, body, _ = call(
            self.app,
            "POST",
            "/auth/signup",
            data={**self._signup_data("new@example.com"), "ignored": "value"},
        )

        self.assertEqual(201, status)
        self.assertEqual(["USER"], body["data"]["roles"])
        self.assertEqual(
            {"userId", "email", "name", "departmentId", "roles", "createdAt"},
            set(body["data"]),
        )
        stored = self.directory.find_by_email("new@example.com")
        self.assertIsNotNone(stored)
        self.assertEqual({"USER"}, stored.roles)
        self.assertNotEqual("CorrectPass!1", stored.password_hash)

    def test_AC_AUTH_002_duplicate_email_is_409_before_later_checks(self):
        self._signup("duplicate@example.com")
        status, body, _ = call(
            self.app,
            "POST",
            "/auth/signup",
            data=self._signup_data(
                "duplicate@example.com",
                "duplicate@example.com",
                name="duplicate@example.com",
                departmentId=2,
            ),
        )

        self.assertEqual(409, status)
        self.assertEqual("USER-002", body["code"])
        self.assertEqual("이미 사용 중인 이메일입니다.", body["message"])

    def test_AC_AUTH_002_concurrent_same_email_signup_has_one_winner(self):
        email = "concurrent@example.com"
        barrier = Barrier(2)

        class RacingDirectory(UserDirectory):
            def find_by_email(self, candidate: str):
                found = super().find_by_email(candidate)
                if candidate == email:
                    barrier.wait(timeout=5)
                return found

        directory = RacingDirectory()
        directory.seed_department(1, "Engineering")
        directory.seed_role("USER")
        auth = AuthService(
            directory,
            InMemoryCache(self.clock),
            TokenManager("stage-two-test-secret"),
            self.clock,
        )
        outcomes: list[tuple[str, object]] = []

        def signup() -> None:
            try:
                result = auth.signup(email, "CorrectPass!1", "Test User", 1)
            except PublicError as error:
                outcomes.append(("error", error.code))
            else:
                outcomes.append(("ok", result["userId"]))

        workers = [Thread(target=signup), Thread(target=signup)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(1, sum(kind == "ok" for kind, _ in outcomes))
        self.assertEqual([("error", "USER-002")], [item for item in outcomes if item[0] == "error"])
        self.assertEqual(1, sum(user.email == email for user in directory._users.values()))

    def test_AC_AUTH_003_password_equal_to_email_or_name_is_rejected(self):
        for email, password, name in (
            ("equal@example.com", "equal@example.com", "Different Name"),
            ("name@example.com", "ExactlySameName", "ExactlySameName"),
        ):
            with self.subTest(email=email):
                status, body, _ = call(
                    self.app,
                    "POST",
                    "/auth/signup",
                    data=self._signup_data(email, password, name=name),
                )
                self.assertEqual(400, status)
                self.assertEqual("USER-005", body["code"])
                self.assertEqual(
                    "비밀번호는 이메일 또는 이름과 같을 수 없습니다.",
                    body["message"],
                )

    def test_AC_AUTH_004_password_length_11_12_64_65_boundaries(self):
        cases = ((11, 400), (12, 201), (64, 201), (65, 400))
        for length, expected in cases:
            with self.subTest(length=length):
                status, body, _ = call(
                    self.app,
                    "POST",
                    "/auth/signup",
                    data=self._signup_data(f"length{length}@example.com", "p" * length),
                )
                self.assertEqual(expected, status)
                if expected == 400:
                    self.assertEqual("COMMON-002", body["code"])

    def test_AC_AUTH_004_registered_routes_reject_malformed_bodies_as_COMMON_002(self):
        for path, raw in (
            ("/auth/signup", b"{"),
            ("/auth/signup", b"[]"),
            ("/auth/signup", json.dumps({"departmentId": True}).encode()),
            ("/auth/login", json.dumps({"email": 7, "password": "password"}).encode()),
        ):
            with self.subTest(path=path, raw=raw):
                response = self.app.handle(
                    Request(
                        "POST",
                        path,
                        {"Content-Type": "application/json"},
                        raw,
                    )
                )
                body = json.loads(response.body)
                self.assertEqual((400, "COMMON-002"), (response.status, body["code"]))

    def test_AC_SYS_007_signup_reports_only_first_body_violation(self):
        status, body, _ = call(
            self.app,
            "POST",
            "/auth/signup",
            data={"email": 7, "password": 8, "name": "", "departmentId": True},
        )

        self.assertEqual((400, "COMMON-002"), (status, body["code"]))
        self.assertEqual("email: 필수 문자열이며 공백일 수 없습니다.", body["message"])
        self.assertNotIn("password", body["message"])

    def test_AC_AUTH_005_inactive_department_is_DEPT_001_400(self):
        status, body, _ = call(
            self.app,
            "POST",
            "/auth/signup",
            data=self._signup_data("inactive-dept@example.com", departmentId=2),
        )

        self.assertEqual(400, status)
        self.assertEqual("DEPT-001", body["code"])
        self.assertEqual("존재하지 않는 부서입니다.", body["message"])
        self.assertIsNone(self.directory.find_by_email("inactive-dept@example.com"))

    def test_AC_AUTH_006_inactive_user_with_correct_password_is_403(self):
        user = self._signup("inactive@example.com")
        self.directory.set_status(user.id, "INACTIVE")

        status, body, _ = call(
            self.app,
            "POST",
            "/auth/login",
            data={"email": user.email, "password": "CorrectPass!1"},
        )

        self.assertEqual(403, status)
        self.assertEqual("USER-003", body["code"])
        self.assertEqual("비활성화된 계정입니다.", body["message"])

    def test_AC_AUTH_007_bad_password_is_401_before_inactive_status(self):
        user = self._signup("inactive-wrong@example.com")
        self.directory.set_status(user.id, "INACTIVE")

        status, body, _ = call(
            self.app,
            "POST",
            "/auth/login",
            data={"email": user.email, "password": "WrongPassword!"},
        )

        self.assertEqual(401, status)
        self.assertEqual("USER-004", body["code"])
        self.assertEqual("이메일 또는 비밀번호가 올바르지 않습니다.", body["message"])

    def test_AC_AUTH_008_successful_login_updates_last_login_and_me(self):
        user = self._signup("login@example.com")
        self.clock.advance(5)
        status, login_body, _ = call(
            self.app,
            "POST",
            "/auth/login",
            data={"email": user.email, "password": "CorrectPass!1"},
        )
        token = login_body["data"]["accessToken"]
        status, me_body, _ = call(self.app, "GET", "/auth/me", token=token)

        self.assertEqual(200, status)
        self.assertEqual(self.clock().isoformat(), me_body["data"]["lastLoginAt"])
        self.assertEqual("Bearer", login_body["data"]["tokenType"])
        self.assertEqual(3600, login_body["data"]["expiresIn"])
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        self.assertNotIn("roles", claims)

    def test_AC_AUTH_009_failed_login_does_not_update_last_login(self):
        user = self._signup("failed-login@example.com")
        previous = self.clock() - timedelta(days=1)
        user.last_login_at = previous
        token = self.tokens.issue(user.id, user.email, self.clock())

        status, _, _ = call(
            self.app,
            "POST",
            "/auth/login",
            data={"email": user.email, "password": "WrongPassword!"},
        )
        me_status, me_body, _ = call(self.app, "GET", "/auth/me", token=token)

        self.assertEqual(401, status)
        self.assertEqual(200, me_status)
        self.assertEqual(previous.isoformat(), me_body["data"]["lastLoginAt"])

    def test_AC_AUTH_010_corrupt_token_logout_is_204_without_revocation(self):
        status, body, raw = call(self.app, "POST", "/auth/logout", token="corrupt.token")

        self.assertEqual(204, status)
        self.assertIsNone(body)
        self.assertEqual(b"", raw)
        self.assertEqual(0, self.cache.size)

    def test_AC_AUTH_011_logout_revokes_token_for_protected_api(self):
        user = self._signup("logout@example.com")
        token = self._login_token(user.email)

        logout_status, _, raw = call(self.app, "POST", "/auth/logout", token=token)
        status, body, _ = call(self.app, "GET", "/protected", token=token)

        self.assertEqual(204, logout_status)
        self.assertEqual(b"", raw)
        self.assertEqual(401, status)
        self.assertEqual("COMMON-007", body["code"])

    def test_AC_AUTH_012_cache_outage_is_fail_open_for_logged_out_token(self):
        user = self._signup("fail-open@example.com")
        token = self._login_token(user.email)
        call(self.app, "POST", "/auth/logout", token=token)
        self.cache.available = False

        status, body, _ = call(self.app, "GET", "/protected", token=token)

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, body["data"])

    def test_AC_AUTH_013_role_revocation_is_visible_no_later_than_30_seconds(self):
        user = self._signup("former-admin@example.com")
        self.directory.assign_role(user.id, "ADMIN")
        token = self._login_token(user.email)
        stale_cache = InMemoryCache(self.clock)
        other_instance = AuthService(
            self.directory,
            stale_cache,
            self.tokens,
            self.clock,
            listen_for_role_changes=False,
        )
        other_app = self._app(other_instance)

        warm_status, _, _ = call(other_app, "GET", "/admin/probe", token=token)
        self.directory.remove_role(user.id, "ADMIN")
        self.clock.advance(29)
        stale_status, _, _ = call(other_app, "GET", "/admin/probe", token=token)
        self.clock.advance(1)
        expired_status, body, _ = call(other_app, "GET", "/admin/probe", token=token)

        self.assertEqual(200, warm_status)
        self.assertEqual(200, stale_status)
        self.assertEqual(403, expired_status)
        self.assertEqual("ROLE-002", body["code"])

    def test_AC_AUTH_013_stale_role_lookup_cannot_refill_cache_after_revocation(self):
        barrier = Barrier(2)

        class RacingDirectory(UserDirectory):
            pause_next_roles = False

            def roles_for(self, user_id: int) -> list[str]:
                roles = super().roles_for(user_id)
                if self.pause_next_roles:
                    self.pause_next_roles = False
                    barrier.wait(timeout=5)
                    barrier.wait(timeout=5)
                return roles

        directory = RacingDirectory()
        directory.seed_department(1, "Engineering")
        directory.seed_role("USER")
        directory.seed_role("ADMIN")
        cache = InMemoryCache(self.clock)
        auth = AuthService(
            directory,
            cache,
            TokenManager("stage-two-test-secret"),
            self.clock,
        )
        user_id = auth.signup(
            "role-race@example.com", "CorrectPass!1", "Test User", 1
        )["userId"]
        directory.assign_role(user_id, "ADMIN")
        token = auth.login("role-race@example.com", "CorrectPass!1")["accessToken"]
        request = Request(
            "GET", "/admin/probe", {"Authorization": f"Bearer {token}"}
        )
        resolved = []

        directory.pause_next_roles = True
        worker = Thread(target=lambda: resolved.append(auth.resolve_request(request)))
        worker.start()
        barrier.wait(timeout=5)
        directory.remove_role(user_id, "ADMIN")
        self.clock.advance(31)
        barrier.wait(timeout=5)
        worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(resolved))
        self.assertNotIn("ADMIN", resolved[0].roles)
        self.assertNotIn("ADMIN", auth.resolve_request(request).roles)
        self.assertEqual('["USER"]', cache.get(f"roles:{user_id}"))


if __name__ == "__main__":
    unittest.main()
