from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from typing import Callable

from src.shared import Department, PublicError, Role, RoleAssignment, UserRecord
from src.shared.identity import IdentityLedgerStore


class InMemoryIdentityStore:
    """Default live-row state behind the identity ledger seam."""

    def __init__(self) -> None:
        self._departments: dict[int, Department] = {}
        self._roles: dict[str, Role] = {}
        self._users: dict[int, UserRecord] = {}
        self._assignments: dict[tuple[int, str], RoleAssignment] = {}
        self._by_email: dict[str, int] = {}
        self._next_user_id = 1
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            yield

    def upsert_department(self, department: Department) -> None:
        with self._lock:
            self._departments[department.id] = department

    def upsert_role(self, role: Role) -> None:
        with self._lock:
            self._roles[role.code] = role

    def find_by_email(self, email: str) -> UserRecord | None:
        with self._lock:
            user_id = self._by_email.get(email)
            return self._users.get(user_id) if user_id is not None else None

    def get_user(
        self, user_id: int, *, for_update: bool = False
    ) -> UserRecord | None:
        del for_update
        with self._lock:
            return self._users.get(user_id)

    def get_department(self, department_id: int) -> Department | None:
        with self._lock:
            return self._departments.get(department_id)

    def get_role(self, role_code: str) -> Role | None:
        with self._lock:
            return self._roles.get(role_code)

    def list_departments(self) -> tuple[Department, ...]:
        with self._lock:
            return tuple(self._departments.values())

    def list_roles(self) -> tuple[Role, ...]:
        with self._lock:
            return tuple(self._roles.values())

    def list_users(self) -> tuple[UserRecord, ...]:
        with self._lock:
            return tuple(self._users.values())

    def roles_for(self, user_id: int) -> tuple[str, ...]:
        with self._lock:
            user = self._users.get(user_id)
            return tuple(user.roles) if user is not None else ()

    def insert_user(
        self,
        email: str,
        password_hash: str,
        name: str,
        department_id: int,
        status: str,
        created_at: datetime,
    ) -> UserRecord | None:
        with self._lock:
            if email in self._by_email:
                return None
            user = UserRecord(
                id=self._next_user_id,
                email=email,
                password_hash=password_hash,
                name=name,
                department_id=department_id,
                status=status,
                created_at=created_at,
            )
            self._next_user_id += 1
            self._users[user.id] = user
            self._by_email[email] = user.id
            return user

    def update_user_status(self, user_id: int, status: str) -> bool:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user.status = status
            return True

    def update_last_login(self, user_id: int, now: datetime) -> bool:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user.last_login_at = now
            return True

    def update_department(self, user_id: int, department_id: int) -> bool:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user.department_id = department_id
            return True

    def get_role_assignment(
        self,
        user_id: int,
        role_code: str,
        *,
        for_update: bool = False,
    ) -> RoleAssignment | None:
        del for_update
        with self._lock:
            return self._assignments.get((user_id, role_code))

    def insert_role_assignment(self, assignment: RoleAssignment) -> bool:
        with self._lock:
            key = (assignment.user_id, assignment.role_code)
            if key in self._assignments:
                return False
            user = self._users[assignment.user_id]
            self._assignments[key] = assignment
            user.roles.add(assignment.role_code)
            return True

    def delete_role_assignment(self, user_id: int, role_code: str) -> bool:
        with self._lock:
            user = self._users.get(user_id)
            had_live_role = user is not None and role_code in user.roles
            assignment = self._assignments.pop((user_id, role_code), None)
            if user is not None:
                user.roles.discard(role_code)
            return assignment is not None or had_live_role


class UserDirectory:
    """Identity rules over an injected state-only ledger."""

    def __init__(self, store: IdentityLedgerStore | None = None) -> None:
        self._store = store if store is not None else InMemoryIdentityStore()
        self._role_change_listeners: list[Callable[[int], None]] = []
        self._lock = getattr(self._store, "_lock", RLock())

    def _memory_store(self) -> InMemoryIdentityStore:
        if not isinstance(self._store, InMemoryIdentityStore):
            raise AttributeError("in-memory compatibility state is unavailable")
        return self._store

    @property
    def _departments(self) -> dict[int, Department]:
        return self._memory_store()._departments

    @property
    def _roles(self) -> dict[str, Role]:
        return self._memory_store()._roles

    @property
    def _users(self) -> dict[int, UserRecord]:
        return self._memory_store()._users

    @property
    def _assignments(self) -> dict[tuple[int, str], RoleAssignment]:
        return self._memory_store()._assignments

    @property
    def _by_email(self) -> dict[str, int]:
        return self._memory_store()._by_email

    @property
    def _next_user_id(self) -> int:
        return self._memory_store()._next_user_id

    @_next_user_id.setter
    def _next_user_id(self, value: int) -> None:
        self._memory_store()._next_user_id = value

    def seed_department(
        self, department_id: int, name: str, status: str = "ACTIVE"
    ) -> None:
        self._store.upsert_department(Department(department_id, name, status))

    def seed_role(
        self, code: str, name: str | None = None, status: str = "ACTIVE"
    ) -> None:
        self._store.upsert_role(Role(code, name or code, status))

    def on_role_change(self, listener: Callable[[int], None]) -> None:
        with self._lock:
            self._role_change_listeners.append(listener)

    def find_by_email(self, email: str) -> UserRecord | None:
        return self._store.find_by_email(email)

    def get_user(self, user_id: int) -> UserRecord | None:
        return self._store.get_user(user_id)

    def get_department(self, department_id: int) -> Department | None:
        return self._store.get_department(department_id)

    def get_role(self, role_code: str) -> Role | None:
        return self._store.get_role(role_code)

    def roles_for(self, user_id: int) -> list[str]:
        return sorted(self._store.roles_for(user_id))

    def create_user(
        self,
        email: str,
        password_hash: str,
        name: str,
        department_id: int,
        now: datetime,
    ) -> UserRecord:
        with self._store.transaction():
            if self._store.find_by_email(email) is not None:
                raise PublicError("USER-002")
            user = self._store.insert_user(
                email,
                password_hash,
                name,
                department_id,
                "ACTIVE",
                now,
            )
            if user is None:
                raise PublicError("USER-002")
            inserted = self._store.insert_role_assignment(
                RoleAssignment(user.id, "USER", user.id, now)
            )
            if not inserted:
                raise RuntimeError("default USER role assignment conflict")
            stored = self._store.get_user(user.id)
            if stored is None:
                raise RuntimeError("created user is unavailable")
            return stored

    def set_status(self, user_id: int, status: str) -> None:
        with self._store.transaction():
            self._require_user(user_id, for_update=True)
            if not self._store.update_user_status(user_id, status):
                raise PublicError("USER-001")

    def record_login(self, user_id: int, now: datetime) -> None:
        with self._store.transaction():
            self._require_user(user_id, for_update=True)
            if not self._store.update_last_login(user_id, now):
                raise PublicError("USER-001")

    def departments_data(self) -> list[dict[str, object]]:
        return [
            {"departmentId": item.id, "name": item.name, "status": item.status}
            for item in sorted(
                self._store.list_departments(), key=lambda value: value.id
            )
        ]

    def roles_data(self) -> list[dict[str, str]]:
        return [
            {"code": item.code, "name": item.name}
            for item in sorted(
                self._store.list_roles(), key=lambda value: value.code
            )
        ]

    def users_data(self) -> list[dict[str, object]]:
        with self._store.transaction():
            return [
                self._user_data(user)
                for user in sorted(
                    self._store.list_users(), key=lambda value: value.id
                )
            ]

    def search_users(self, keyword: str) -> tuple[dict[str, object], ...]:
        """Project permission-target candidates by case-insensitive name or email."""

        needle = keyword.strip().casefold()
        if not needle:
            return ()
        with self._store.transaction():
            matches = tuple(
                user
                for user in sorted(
                    self._store.list_users(), key=lambda value: value.id
                )
                if needle in user.email.casefold() or needle in user.name.casefold()
            )
            return tuple(
                {
                    "userId": data["userId"],
                    "email": data["email"],
                    "name": data["name"],
                    "departmentId": data["departmentId"],
                    "departmentName": data["departmentName"],
                }
                for user in matches
                for data in (self._user_data(user),)
            )

    def user_data(self, user_id: int) -> dict[str, object]:
        with self._store.transaction():
            return self._user_data(self._require_user(user_id))

    def _user_data(self, user: UserRecord) -> dict[str, object]:
        department = (
            self._store.get_department(user.department_id)
            if user.department_id is not None
            else None
        )
        return {
            "userId": user.id,
            "email": user.email,
            "name": user.name,
            "nickname": user.nickname,
            "profileImageUrl": user.profile_image_url,
            "departmentId": user.department_id,
            "departmentName": department.name if department else None,
            "roles": sorted(self._store.roles_for(user.id)),
            "status": user.status,
            "createdAt": user.created_at.isoformat(),
            "lastLoginAt": (
                user.last_login_at.isoformat() if user.last_login_at else None
            ),
        }

    def assign_role(
        self,
        user_id: int,
        role_code: str,
        *,
        granted_by_user_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        with self._store.transaction():
            user = self._require_user(user_id, for_update=True)
            if self._store.get_role(role_code) is None:
                raise PublicError("ROLE-001")
            if role_code in user.roles:
                raise PublicError("ROLE-003")
            inserted = self._store.insert_role_assignment(
                RoleAssignment(
                    user_id,
                    role_code,
                    granted_by_user_id
                    if granted_by_user_id is not None
                    else user_id,
                    now or datetime.now(UTC),
                )
            )
            if not inserted:
                raise PublicError("ROLE-003")
        self._notify_role_change(user_id)

    def remove_role(self, user_id: int, role_code: str) -> None:
        with self._store.transaction():
            user = self._require_user(user_id, for_update=True)
            if role_code not in user.roles:
                raise PublicError("ROLE-004")
            if not self._store.delete_role_assignment(user_id, role_code):
                raise PublicError("ROLE-004")
        self._notify_role_change(user_id)

    def role_assignment(
        self, user_id: int, role_code: str
    ) -> RoleAssignment | None:
        return self._store.get_role_assignment(user_id, role_code)

    def change_department(self, user_id: int, department_id: int) -> None:
        with self._store.transaction():
            self._require_user(user_id, for_update=True)
            department = self._store.get_department(department_id)
            if department is None or department.status != "ACTIVE":
                raise PublicError("DEPT-001")
            if not self._store.update_department(user_id, department_id):
                raise PublicError("USER-001")

    def _require_user(
        self, user_id: int, *, for_update: bool = False
    ) -> UserRecord:
        user = self._store.get_user(user_id, for_update=for_update)
        if user is None:
            raise PublicError("USER-001")
        return user

    def _notify_role_change(self, user_id: int) -> None:
        with self._lock:
            listeners = tuple(self._role_change_listeners)
        for listener in listeners:
            listener(user_id)
