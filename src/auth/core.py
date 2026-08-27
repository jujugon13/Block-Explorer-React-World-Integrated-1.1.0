from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import uuid
from datetime import UTC, datetime
from threading import RLock
from typing import Callable

from src.shared import CacheStore, IdentityDirectory, Principal, PublicError, Request

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class InMemoryCache:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._values: dict[str, tuple[str, float]] = {}
        self.available = True

    def get(self, key: str) -> str | None:
        self._check()
        item = self._values.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at <= self._clock().timestamp():
            self._values.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._check()
        self._values[key] = (value, self._clock().timestamp() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._check()
        self._values.pop(key, None)

    def _check(self) -> None:
        if not self.available:
            raise ConnectionError("cache unavailable")

    @property
    def size(self) -> int:
        return len(self._values)


class TokenManager:
    def __init__(self, secret: str, expiration_seconds: int = 3600) -> None:
        if not secret:
            raise ValueError("JWT secret is required")
        self._secret = secret.encode()
        self.expiration_seconds = expiration_seconds

    def issue(self, user_id: int, email: str, now: datetime) -> str:
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = _b64(
            json.dumps(
                {
                    "sub": email,
                    "userId": user_id,
                    "jti": str(uuid.uuid4()),
                    "iat": int(now.timestamp()),
                    "exp": int(now.timestamp()) + self.expiration_seconds,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        signed = f"{header}.{payload}"
        signature = _b64(hmac.new(self._secret, signed.encode(), hashlib.sha256).digest())
        return f"{signed}.{signature}"

    def validate(self, token: str, now: datetime) -> dict[str, object] | None:
        try:
            header, payload, signature = token.split(".")
            if json.loads(_unb64(header)) != {"alg": "HS256", "typ": "JWT"}:
                return None
            signed = f"{header}.{payload}"
            expected = _b64(hmac.new(self._secret, signed.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            claims = json.loads(_unb64(payload))
            if int(claims["exp"]) <= int(now.timestamp()):
                return None
            if not isinstance(claims.get("sub"), str) or not isinstance(claims.get("userId"), int):
                return None
            if not isinstance(claims.get("jti"), str):
                return None
            uuid.UUID(str(claims["jti"]))
            return claims
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None


def _hash_password(password: str) -> str:
    iterations = 200_000
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(("pbkdf2_sha256", str(iterations), _b64(salt), _b64(digest)))


def _password_matches(password: str, encoded: str) -> bool:
    try:
        _, iterations, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(_b64(actual), expected)
    except (ValueError, TypeError):
        return False


class AuthService:
    def __init__(
        self,
        directory: IdentityDirectory,
        cache: CacheStore,
        token_manager: TokenManager,
        clock: Callable[[], datetime] | None = None,
        *,
        listen_for_role_changes: bool = True,
    ) -> None:
        self.directory = directory
        self.cache = cache
        self.tokens = token_manager
        self.clock = clock or (lambda: datetime.now(UTC))
        self._role_cache_lock = RLock()
        self._role_cache_generations: dict[int, int] = {}
        if listen_for_role_changes and hasattr(directory, "on_role_change"):
            directory.on_role_change(self.invalidate_roles)

    def signup(self, email: str, password: str, name: str, department_id: int, **_extra) -> dict[str, object]:
        if not email or not EMAIL.fullmatch(email) or not name or not name.strip():
            raise PublicError("COMMON-002")
        if not isinstance(password, str) or not 12 <= len(password) <= 64 or not password.strip():
            raise PublicError("COMMON-002")
        if self.directory.find_by_email(email):
            raise PublicError("USER-002")
        if password == email or password == name:
            raise PublicError("USER-005")
        department = self.directory.get_department(department_id)
        if department is None or department.status != "ACTIVE":
            raise PublicError("DEPT-001")
        role = self.directory.get_role("USER")
        if role is None or getattr(role, "status", "ACTIVE") != "ACTIVE":
            raise PublicError("ROLE-001")
        user = self.directory.create_user(email, _hash_password(password), name, department_id, self.clock())
        return {
            "userId": user.id,
            "email": user.email,
            "name": user.name,
            "departmentId": user.department_id,
            "roles": ["USER"],
            "createdAt": user.created_at.isoformat(),
        }

    def login(self, email: str, password: str) -> dict[str, object]:
        user = self.directory.find_by_email(email)
        if user is None or not _password_matches(password, user.password_hash):
            raise PublicError("USER-004")
        if user.status != "ACTIVE":
            raise PublicError("USER-003")
        now = self.clock()
        record_login = getattr(self.directory, "record_login", None)
        if callable(record_login):
            record_login(user.id, now)
        else:
            user.last_login_at = now
        roles = self.directory.roles_for(user.id)
        return {
            "accessToken": self.tokens.issue(user.id, user.email, now),
            "tokenType": "Bearer",
            "expiresIn": self.tokens.expiration_seconds,
            "userId": user.id,
            "email": user.email,
            "roles": roles,
        }

    def me(self, user_id: int) -> dict[str, object]:
        if self.directory.get_user(user_id) is None:
            raise PublicError("USER-001")
        return self.directory.user_data(user_id)

    def logout(self, authorization: str | None) -> None:
        token = self._extract(authorization)
        claims = self.tokens.validate(token, self.clock()) if token else None
        if claims is None:
            return
        remaining = int(claims["exp"]) - self.clock().timestamp()
        if remaining > 0:
            try:
                self.cache.set(f"revoked:{claims['jti']}", "1", math.ceil(remaining))
            except ConnectionError:
                pass

    def resolve_request(self, request: Request) -> Principal | None:
        token = self._extract(request.header("authorization"))
        claims = self.tokens.validate(token, self.clock()) if token else None
        if claims is None or self._is_revoked(str(claims["jti"])):
            return None
        user = self.directory.get_user(int(claims["userId"]))
        if user is None:
            return None
        return Principal(
            user.email,
            frozenset(self._roles(user.id)),
            user_id=user.id,
            department_id=user.department_id,
            display_name=user.name,
        )

    def invalidate_roles(self, user_id: int) -> None:
        with self._role_cache_lock:
            self._role_cache_generations[user_id] = (
                self._role_cache_generations.get(user_id, 0) + 1
            )
            try:
                self.cache.delete(f"roles:{user_id}")
            except ConnectionError:
                pass

    def _roles(self, user_id: int) -> list[str]:
        key = f"roles:{user_id}"
        while True:
            with self._role_cache_lock:
                generation = self._role_cache_generations.get(user_id, 0)
            try:
                cached = self.cache.get(key)
                parsed = list(json.loads(cached)) if cached is not None else None
            except (ConnectionError, json.JSONDecodeError):
                parsed = None
            with self._role_cache_lock:
                if generation != self._role_cache_generations.get(user_id, 0):
                    continue
                if parsed is not None:
                    return parsed

            roles = self.directory.roles_for(user_id)
            with self._role_cache_lock:
                if generation != self._role_cache_generations.get(user_id, 0):
                    continue
                try:
                    self.cache.set(key, json.dumps(roles), 30)
                except ConnectionError:
                    pass
                return roles

    def _is_revoked(self, jti: str) -> bool:
        try:
            return self.cache.get(f"revoked:{jti}") is not None
        except ConnectionError:
            return False

    @staticmethod
    def _extract(authorization: str | None) -> str | None:
        return authorization[7:] if authorization and authorization.startswith("Bearer ") else None
