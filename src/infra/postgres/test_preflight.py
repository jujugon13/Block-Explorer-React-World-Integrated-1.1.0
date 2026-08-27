from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from src.infra.postgres.preflight import (
    PostgresCapabilities,
    PostgresCompatibilityError,
    main,
    verify_postgres_capabilities,
)
from src.infra.postgres.transaction import PostgresTransactionManager


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.row = None

    def execute(self, sql: str) -> None:
        self.connection.statements.append(sql)
        if sql.startswith("SELECT current_setting"):
            self.row = self.connection.capabilities

    def fetchone(self):
        return self.row

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, capabilities) -> None:
        self.capabilities = capabilities
        self.statements: list[str] = []

    def cursor(self):
        return _Cursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class PostgresPreflightIntegrationTests(unittest.TestCase):
    def test_IT_PREFLIGHT_001_accepts_only_the_fixed_server_contract(self):
        connection = _Connection(("18.3", "180003", "read committed", "0.8.1"))
        result = verify_postgres_capabilities(
            PostgresTransactionManager(lambda: connection)
        )

        self.assertEqual((180003, "0.8.1"), (
            result.server_version_num,
            result.vector_version,
        ))
        self.assertEqual(
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
            connection.statements[0],
        )

    def test_IT_PREFLIGHT_002_rejects_server_or_extension_drift(self):
        cases = (
            (("18.2", "180002", "read committed", "0.8.1"), "18.3"),
            (("18.3", "180003", "repeatable read", "0.8.1"), "READ COMMITTED"),
            (("18.3", "180003", "read committed", "0.8.0"), "pgvector 0.8.1"),
            (("18.3", "180003", "read committed", None), "not installed"),
        )
        for row, message in cases:
            with self.subTest(row=row), self.assertRaisesRegex(
                PostgresCompatibilityError, message
            ):
                verify_postgres_capabilities(
                    PostgresTransactionManager(lambda row=row: _Connection(row))
                )

    def test_IT_PREFLIGHT_003_command_prints_capabilities_without_config(self):
        output = StringIO()
        capabilities = PostgresCapabilities(
            "18.3", 180003, "0.8.1", "read committed"
        )
        with (
            patch(
                "src.infra.postgres.preflight.PostgresConfig.from_env",
                return_value=object(),
            ),
            patch(
                "src.infra.postgres.preflight.verify_postgres_capabilities",
                return_value=capabilities,
            ),
            redirect_stdout(output),
        ):
            result = main()

        self.assertEqual(0, result)
        self.assertEqual(
            "PostgreSQL 18.3; pgvector 0.8.1; isolation read committed\n",
            output.getvalue(),
        )

    def test_IT_PREFLIGHT_004_command_redacts_unexpected_failure(self):
        output = StringIO()
        with (
            patch(
                "src.infra.postgres.preflight.PostgresConfig.from_env",
                side_effect=OSError("host=private password=private"),
            ),
            redirect_stderr(output),
        ):
            result = main()

        self.assertEqual(1, result)
        self.assertEqual("PostgreSQL capability check failed\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
