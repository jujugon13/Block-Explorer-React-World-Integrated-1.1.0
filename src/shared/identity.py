from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class Department:
    id: int
    name: str
    status: str = "ACTIVE"


@dataclass(slots=True)
class Role:
    code: str
    name: str
    status: str = "ACTIVE"


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    user_id: int
    role_code: str
    granted_by_user_id: int
    granted_at: datetime


@dataclass(slots=True)
class UserRecord:
    id: int
    email: str
    password_hash: str
    name: str
    department_id: int | None
    status: str
    created_at: datetime
    roles: set[str] = field(default_factory=set)
    nickname: str | None = None
    profile_image_url: str | None = None
    last_login_at: datetime | None = None


class IdentityDirectory(Protocol):
    def find_by_email(self, email: str) -> UserRecord | None: ...
    def get_user(self, user_id: int) -> UserRecord | None: ...
    def get_department(self, department_id: int) -> Department | None: ...
    def get_role(self, role_code: str) -> Role | None: ...
    def roles_for(self, user_id: int) -> list[str]: ...
    def create_user(
        self,
        email: str,
        password_hash: str,
        name: str,
        department_id: int,
        now: datetime,
    ) -> UserRecord: ...
    def record_login(self, user_id: int, now: datetime) -> None: ...
    def user_data(self, user_id: int) -> dict[str, object]: ...
    def on_role_change(self, listener: Callable[[int], None]) -> None: ...


class IdentityLedgerStore(Protocol):
    """State-only identity ledger used behind the feature-owned rules."""

    def transaction(self) -> AbstractContextManager[None]: ...

    def upsert_department(self, department: Department) -> None: ...
    def upsert_role(self, role: Role) -> None: ...

    def find_by_email(self, email: str) -> UserRecord | None: ...
    def get_user(
        self, user_id: int, *, for_update: bool = False
    ) -> UserRecord | None: ...
    def get_department(self, department_id: int) -> Department | None: ...
    def get_role(self, role_code: str) -> Role | None: ...
    def list_departments(self) -> tuple[Department, ...]: ...
    def list_roles(self) -> tuple[Role, ...]: ...
    def list_users(self) -> tuple[UserRecord, ...]: ...
    def roles_for(self, user_id: int) -> tuple[str, ...]: ...

    def insert_user(
        self,
        email: str,
        password_hash: str,
        name: str,
        department_id: int,
        status: str,
        created_at: datetime,
    ) -> UserRecord | None: ...
    def update_user_status(self, user_id: int, status: str) -> bool: ...
    def update_last_login(self, user_id: int, now: datetime) -> bool: ...
    def update_department(self, user_id: int, department_id: int) -> bool: ...

    def get_role_assignment(
        self,
        user_id: int,
        role_code: str,
        *,
        for_update: bool = False,
    ) -> RoleAssignment | None: ...
    def insert_role_assignment(self, assignment: RoleAssignment) -> bool: ...
    def delete_role_assignment(self, user_id: int, role_code: str) -> bool: ...


class CacheStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    def delete(self, key: str) -> None: ...
