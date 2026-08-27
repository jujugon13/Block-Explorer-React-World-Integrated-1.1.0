from __future__ import annotations

import unittest

from src.infra.postgres.transaction import (
    PostgresConnectionError,
    PostgresTransactionManager,
    TransactionRollbackOnlyError,
)
from src.shared.database import CommitOutcomeUnknown
from src.shared.document_ledger import CommitOutcomeUnknown as DocumentCommitUnknown


class _Disconnected(Exception):
    pass


class _ValidationFailure(Exception):
    pass


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection

    def execute(self, sql: str) -> None:
        self.connection.statements.append(sql)
        if self.connection.execute_error is not None:
            raise self.connection.execute_error

    def close(self) -> None:
        self.connection.cursor_closes += 1


class _Connection:
    def __init__(self, *, commit_error=None, execute_error=None) -> None:
        self.commit_error = commit_error
        self.execute_error = execute_error
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursor_closes = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


class PostgresTransactionManagerTests(unittest.TestCase):
    def test_IT_UOW_001_outer_transaction_sets_isolation_and_commits_once(self):
        connection = _Connection()
        manager = PostgresTransactionManager(lambda: connection)

        with manager.transaction():
            self.assertIs(connection, manager.current_connection())

        self.assertEqual(
            ["SET TRANSACTION ISOLATION LEVEL READ COMMITTED"],
            connection.statements,
        )
        self.assertEqual((1, 0, 1, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
            connection.cursor_closes,
        ))
        with self.assertRaises(RuntimeError):
            manager.current_connection()

    def test_IT_UOW_002_nested_transaction_reuses_one_connection(self):
        connections: list[_Connection] = []

        def factory():
            connection = _Connection()
            connections.append(connection)
            return connection

        manager = PostgresTransactionManager(factory)
        with manager.transaction():
            outer = manager.current_connection()
            with manager.transaction():
                self.assertIs(outer, manager.current_connection())
            self.assertEqual(0, outer.commits)

        self.assertEqual(1, len(connections))
        self.assertEqual((1, 0, 1), (
            outer.commits,
            outer.rollbacks,
            outer.closes,
        ))

    def test_IT_UOW_003_caught_nested_failure_marks_outer_rollback_only(self):
        connection = _Connection()
        manager = PostgresTransactionManager(lambda: connection)

        with self.assertRaises(TransactionRollbackOnlyError):
            with manager.transaction():
                try:
                    with manager.transaction():
                        raise _ValidationFailure()
                except _ValidationFailure:
                    pass

        self.assertEqual((0, 1, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))

    def test_IT_UOW_004_uncaught_body_failure_is_preserved_and_rolled_back(self):
        connection = _Connection()
        manager = PostgresTransactionManager(lambda: connection)
        error = _ValidationFailure()

        with self.assertRaises(_ValidationFailure) as caught:
            with manager.transaction():
                raise error

        self.assertIs(error, caught.exception)
        self.assertEqual((0, 1, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))

    def test_IT_UOW_005_operation_opens_or_reuses_transaction(self):
        connections: list[_Connection] = []

        def factory():
            connection = _Connection()
            connections.append(connection)
            return connection

        manager = PostgresTransactionManager(factory)
        with manager.operation() as first:
            self.assertIs(first, manager.current_connection())
        with manager.transaction():
            ambient = manager.current_connection()
            with manager.operation() as nested:
                self.assertIs(ambient, nested)

        self.assertEqual(2, len(connections))
        self.assertEqual([1, 1], [item.commits for item in connections])
        self.assertEqual([1, 1], [item.closes for item in connections])

    def test_IT_UOW_006_connection_failure_during_commit_is_unknown(self):
        connection = _Connection(commit_error=_Disconnected())
        manager = PostgresTransactionManager(
            lambda: connection,
            is_connection_error=lambda error: isinstance(error, _Disconnected),
        )

        with self.assertRaises(CommitOutcomeUnknown):
            with manager.transaction():
                pass

        self.assertEqual((1, 0, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))
        self.assertIs(CommitOutcomeUnknown, DocumentCommitUnknown)

    def test_IT_UOW_007_non_connection_commit_failure_is_preserved(self):
        error = _ValidationFailure()
        connection = _Connection(commit_error=error)
        manager = PostgresTransactionManager(
            lambda: connection,
            is_connection_error=lambda _: False,
        )

        with self.assertRaises(_ValidationFailure) as caught:
            with manager.transaction():
                pass

        self.assertIs(error, caught.exception)
        self.assertEqual((1, 1, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))

    def test_IT_UOW_008_isolation_sql_failure_is_preserved_without_commit(self):
        error = _ValidationFailure()
        connection = _Connection(execute_error=error)
        manager = PostgresTransactionManager(lambda: connection)

        with self.assertRaises(_ValidationFailure) as caught:
            with manager.transaction():
                pass

        self.assertIs(error, caught.exception)
        self.assertEqual((0, 1, 1), (
            connection.commits,
            connection.rollbacks,
            connection.closes,
        ))

    def test_IT_UOW_009_connection_open_failure_does_not_expose_driver_details(self):
        def unavailable():
            raise OSError("host=private user=private password=private")

        manager = PostgresTransactionManager(unavailable)
        with self.assertRaises(PostgresConnectionError) as caught:
            with manager.transaction():
                pass

        self.assertEqual("PostgreSQL connection failed", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
