from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime

from src.infra.postgres.identity_store import PostgresIdentityStore
from src.shared import Department, Role, RoleAssignment


NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, parameters: object = None) -> None:
        self.connection.calls.append((_normalized(sql), parameters))
        scripted = self.connection.script.pop(0)
        self.rows = list(scripted)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        self.connection.closed_cursors += 1


class _Connection:
    def __init__(self, script: list[list[tuple[object, ...]]]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, object]] = []
        self.closed_cursors = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Transactions:
    def __init__(self, script: list[list[tuple[object, ...]]]) -> None:
        self.connection = _Connection(script)
        self.operations = 0
        self.transactions = 0

    @contextmanager
    def operation(self):
        self.operations += 1
        yield self.connection

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


def _user_row(*, roles=("USER",)) -> tuple[object, ...]:
    return (
        7,
        "CaseSensitive@Example.com",
        "hash",
        "Example",
        3,
        "ACTIVE",
        "nickname",
        None,
        NOW,
        None,
        roles,
    )


class PostgresIdentityStoreIntegrationTests(unittest.TestCase):
    def test_IT_IDENTITY_001_exact_email_query_maps_user_and_role_set(self):
        transactions = _Transactions([[ _user_row(roles=("USER", "ADMIN")) ]])
        store = PostgresIdentityStore(transactions)  # type: ignore[arg-type]

        user = store.find_by_email("CaseSensitive@Example.com")

        self.assertIsNotNone(user)
        self.assertEqual(7, user.id)
        self.assertEqual({"USER", "ADMIN"}, user.roles)
        sql, parameters = transactions.connection.calls[0]
        self.assertIn("WHERE u.email = %s", sql)
        self.assertNotIn("lower(", sql.casefold())
        self.assertNotIn("ilike", sql.casefold())
        self.assertEqual(("CaseSensitive@Example.com",), parameters)
        self.assertEqual((1, 1), (
            transactions.operations,
            transactions.connection.closed_cursors,
        ))

    def test_IT_IDENTITY_002_for_update_locks_user_and_assignment_rows(self):
        transactions = _Transactions([
            [_user_row()],
            [(7, "ADMIN", 1, NOW)],
        ])
        store = PostgresIdentityStore(transactions)  # type: ignore[arg-type]

        user = store.get_user(7, for_update=True)
        assignment = store.get_role_assignment(7, "ADMIN", for_update=True)

        self.assertEqual(7, user.id)
        self.assertEqual("ADMIN", assignment.role_code)
        user_sql, _ = transactions.connection.calls[0]
        assignment_sql, _ = transactions.connection.calls[1]
        self.assertIn("FOR UPDATE OF u", user_sql)
        self.assertTrue(assignment_sql.endswith("FOR UPDATE"))

    def test_IT_IDENTITY_003_expected_duplicates_use_do_nothing_returning(self):
        transactions = _Transactions([[], []])
        store = PostgresIdentityStore(transactions)  # type: ignore[arg-type]

        user = store.insert_user(
            "duplicate@example.com",
            "hash",
            "Duplicate",
            3,
            "ACTIVE",
            NOW,
        )
        assignment = store.insert_role_assignment(
            RoleAssignment(7, "USER", 7, NOW)
        )

        self.assertIsNone(user)
        self.assertFalse(assignment)
        user_sql, _ = transactions.connection.calls[0]
        role_sql, _ = transactions.connection.calls[1]
        self.assertIn(
            "ON CONFLICT ON CONSTRAINT uq_users_email DO NOTHING", user_sql
        )
        self.assertIn("RETURNING user_id", user_sql)
        self.assertIn(
            "ON CONFLICT ON CONSTRAINT pk_user_roles DO NOTHING", role_sql
        )
        self.assertIn("RETURNING user_id", role_sql)

    def test_IT_IDENTITY_004_crud_uses_explicit_rows_and_updates(self):
        transactions = _Transactions([
            [],
            [],
            [(7,)],
            [],
            [(7,)],
            [(7,)],
        ])
        store = PostgresIdentityStore(transactions)  # type: ignore[arg-type]

        store.upsert_department(Department(3, "Engineering"))
        store.upsert_role(Role("USER", "User"))
        self.assertTrue(store.update_user_status(7, "INACTIVE"))
        self.assertFalse(store.update_last_login(99, NOW))
        self.assertTrue(store.update_department(7, 3))
        self.assertTrue(store.delete_role_assignment(7, "USER"))

        statements = [call[0] for call in transactions.connection.calls]
        self.assertTrue(statements[0].startswith("INSERT INTO departments"))
        self.assertTrue(statements[1].startswith("INSERT INTO roles"))
        self.assertIn("UPDATE users SET status = %s", statements[2])
        self.assertIn("UPDATE users SET last_login_at = %s", statements[3])
        self.assertIn("UPDATE users SET department_id = %s", statements[4])
        self.assertTrue(statements[5].startswith("DELETE FROM user_roles"))

    def test_IT_IDENTITY_005_transaction_delegates_without_opening_operation(self):
        transactions = _Transactions([])
        store = PostgresIdentityStore(transactions)  # type: ignore[arg-type]

        with store.transaction():
            pass

        self.assertEqual((1, 0), (
            transactions.transactions,
            transactions.operations,
        ))


if __name__ == "__main__":
    unittest.main()
