"""PostgreSQL row persistence for the shared identity ledger contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.shared import Department, Role, RoleAssignment, UserRecord

from .transaction import PostgresTransactionManager


_USER_COLUMNS = """
    u.user_id,
    u.email,
    u.password_hash,
    u.name,
    u.department_id,
    u.status,
    u.nickname,
    u.profile_image_url,
    u.created_at,
    u.last_login_at,
    ARRAY(
        SELECT ur.role_code
        FROM user_roles AS ur
        WHERE ur.user_id = u.user_id
        ORDER BY ur.role_code
    ) AS roles
"""


def _close(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


def _run(connection: Any, sql: str, parameters: tuple[object, ...]) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
    finally:
        _close(cursor)


def _fetchone(
    connection: Any, sql: str, parameters: tuple[object, ...]
) -> object | None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return cursor.fetchone()
    finally:
        _close(cursor)


def _fetchall(
    connection: Any, sql: str, parameters: tuple[object, ...] = ()
) -> list[object]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return list(cursor.fetchall())
    finally:
        _close(cursor)


def _department(row: object) -> Department:
    values = tuple(row)  # type: ignore[arg-type]
    return Department(int(values[0]), str(values[1]), str(values[2]))


def _role(row: object) -> Role:
    values = tuple(row)  # type: ignore[arg-type]
    return Role(str(values[0]), str(values[1]), str(values[2]))


def _user(row: object) -> UserRecord:
    values = tuple(row)  # type: ignore[arg-type]
    department_id = None if values[4] is None else int(values[4])
    return UserRecord(
        id=int(values[0]),
        email=str(values[1]),
        password_hash=str(values[2]),
        name=str(values[3]),
        department_id=department_id,
        status=str(values[5]),
        created_at=values[8],  # type: ignore[arg-type]
        roles={str(item) for item in (values[10] or ())},
        nickname=None if values[6] is None else str(values[6]),
        profile_image_url=None if values[7] is None else str(values[7]),
        last_login_at=values[9],  # type: ignore[arg-type]
    )


def _assignment(row: object) -> RoleAssignment:
    values = tuple(row)  # type: ignore[arg-type]
    return RoleAssignment(
        int(values[0]),
        str(values[1]),
        int(values[2]),
        values[3],  # type: ignore[arg-type]
    )


class PostgresIdentityStore:
    """SQL-only implementation of the shared identity ledger store."""

    def __init__(self, transactions: PostgresTransactionManager) -> None:
        self._transactions = transactions

    def transaction(self):
        return self._transactions.transaction()

    def upsert_department(self, department: Department) -> None:
        sql = """
            INSERT INTO departments (department_id, name, status)
            VALUES (%s, %s, %s)
            ON CONFLICT ON CONSTRAINT pk_departments DO UPDATE
            SET name = EXCLUDED.name,
                status = EXCLUDED.status
        """
        with self._transactions.operation() as connection:
            _run(connection, sql, (department.id, department.name, department.status))

    def upsert_role(self, role: Role) -> None:
        sql = """
            INSERT INTO roles (role_code, name, status)
            VALUES (%s, %s, %s)
            ON CONFLICT ON CONSTRAINT pk_roles DO UPDATE
            SET name = EXCLUDED.name,
                status = EXCLUDED.status
        """
        with self._transactions.operation() as connection:
            _run(connection, sql, (role.code, role.name, role.status))

    def find_by_email(self, email: str) -> UserRecord | None:
        sql = f"""
            SELECT {_USER_COLUMNS}
            FROM users AS u
            WHERE u.email = %s
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (email,))
        return None if row is None else _user(row)

    def get_user(
        self, user_id: int, *, for_update: bool = False
    ) -> UserRecord | None:
        lock = " FOR UPDATE OF u" if for_update else ""
        sql = f"""
            SELECT {_USER_COLUMNS}
            FROM users AS u
            WHERE u.user_id = %s{lock}
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (user_id,))
        return None if row is None else _user(row)

    def get_department(self, department_id: int) -> Department | None:
        sql = """
            SELECT department_id, name, status
            FROM departments
            WHERE department_id = %s
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (department_id,))
        return None if row is None else _department(row)

    def get_role(self, role_code: str) -> Role | None:
        sql = """
            SELECT role_code, name, status
            FROM roles
            WHERE role_code = %s
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (role_code,))
        return None if row is None else _role(row)

    def list_departments(self) -> tuple[Department, ...]:
        sql = """
            SELECT department_id, name, status
            FROM departments
            ORDER BY department_id
        """
        with self._transactions.operation() as connection:
            rows = _fetchall(connection, sql)
        return tuple(_department(row) for row in rows)

    def list_roles(self) -> tuple[Role, ...]:
        sql = """
            SELECT role_code, name, status
            FROM roles
            ORDER BY role_code
        """
        with self._transactions.operation() as connection:
            rows = _fetchall(connection, sql)
        return tuple(_role(row) for row in rows)

    def list_users(self) -> tuple[UserRecord, ...]:
        sql = f"""
            SELECT {_USER_COLUMNS}
            FROM users AS u
            ORDER BY u.user_id
        """
        with self._transactions.operation() as connection:
            rows = _fetchall(connection, sql)
        return tuple(_user(row) for row in rows)

    def roles_for(self, user_id: int) -> tuple[str, ...]:
        sql = """
            SELECT role_code
            FROM user_roles
            WHERE user_id = %s
            ORDER BY role_code
        """
        with self._transactions.operation() as connection:
            rows = _fetchall(connection, sql, (user_id,))
        return tuple(str(tuple(row)[0]) for row in rows)  # type: ignore[arg-type]

    def insert_user(
        self,
        email: str,
        password_hash: str,
        name: str,
        department_id: int,
        status: str,
        created_at: datetime,
    ) -> UserRecord | None:
        sql = """
            INSERT INTO users (
                email,
                password_hash,
                name,
                department_id,
                status,
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT uq_users_email DO NOTHING
            RETURNING
                user_id,
                email,
                password_hash,
                name,
                department_id,
                status,
                nickname,
                profile_image_url,
                created_at,
                last_login_at,
                ARRAY[]::text[] AS roles
        """
        parameters = (
            email,
            password_hash,
            name,
            department_id,
            status,
            created_at,
        )
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, parameters)
        return None if row is None else _user(row)

    def update_user_status(self, user_id: int, status: str) -> bool:
        sql = """
            UPDATE users
            SET status = %s
            WHERE user_id = %s
            RETURNING user_id
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (status, user_id))
        return row is not None

    def update_last_login(self, user_id: int, now: datetime) -> bool:
        sql = """
            UPDATE users
            SET last_login_at = %s
            WHERE user_id = %s
            RETURNING user_id
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (now, user_id))
        return row is not None

    def update_department(self, user_id: int, department_id: int) -> bool:
        sql = """
            UPDATE users
            SET department_id = %s
            WHERE user_id = %s
            RETURNING user_id
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (department_id, user_id))
        return row is not None

    def get_role_assignment(
        self,
        user_id: int,
        role_code: str,
        *,
        for_update: bool = False,
    ) -> RoleAssignment | None:
        lock = " FOR UPDATE" if for_update else ""
        sql = f"""
            SELECT user_id, role_code, granted_by_user_id, granted_at
            FROM user_roles
            WHERE user_id = %s AND role_code = %s{lock}
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (user_id, role_code))
        return None if row is None else _assignment(row)

    def insert_role_assignment(self, assignment: RoleAssignment) -> bool:
        sql = """
            INSERT INTO user_roles (
                user_id,
                role_code,
                granted_by_user_id,
                granted_at
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT pk_user_roles DO NOTHING
            RETURNING user_id
        """
        parameters = (
            assignment.user_id,
            assignment.role_code,
            assignment.granted_by_user_id,
            assignment.granted_at,
        )
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, parameters)
        return row is not None

    def delete_role_assignment(self, user_id: int, role_code: str) -> bool:
        sql = """
            DELETE FROM user_roles
            WHERE user_id = %s AND role_code = %s
            RETURNING user_id
        """
        with self._transactions.operation() as connection:
            row = _fetchone(connection, sql, (user_id, role_code))
        return row is not None
