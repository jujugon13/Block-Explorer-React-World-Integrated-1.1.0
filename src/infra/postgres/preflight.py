"""PostgreSQL capability checks required before adapter assembly."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import partial

from .config import (
    PostgresConfig,
    PostgresConfigurationError,
    PostgresDependencyError,
    connect,
)
from .transaction import PostgresTransactionManager


EXPECTED_SERVER_VERSION_NUM = 180003
EXPECTED_VECTOR_VERSION = "0.8.1"


class PostgresCompatibilityError(RuntimeError):
    """The connected server cannot satisfy the fixed deployment contract."""


@dataclass(frozen=True, slots=True)
class PostgresCapabilities:
    server_version: str
    server_version_num: int
    vector_version: str
    transaction_isolation: str


def verify_postgres_capabilities(
    transactions: PostgresTransactionManager,
) -> PostgresCapabilities:
    """Verify PostgreSQL 18.3, pgvector 0.8.1, and READ COMMITTED."""

    with transactions.operation() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT current_setting('server_version'), "
                "current_setting('server_version_num'), "
                "current_setting('transaction_isolation'), "
                "(SELECT extversion FROM pg_extension WHERE extname = 'vector')"
            )
            row = cursor.fetchone()
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    if row is None or len(row) != 4:
        raise PostgresCompatibilityError("PostgreSQL capability query returned no row")
    try:
        version_num = int(row[1])
    except (TypeError, ValueError):
        raise PostgresCompatibilityError(
            "PostgreSQL server version is unavailable"
        ) from None
    server_version = str(row[0])
    isolation = str(row[2]).casefold()
    vector_version = "" if row[3] is None else str(row[3])
    if version_num != EXPECTED_SERVER_VERSION_NUM:
        raise PostgresCompatibilityError(
            f"PostgreSQL 18.3 is required; connected server is {server_version}"
        )
    if isolation != "read committed":
        raise PostgresCompatibilityError(
            f"READ COMMITTED is required; transaction uses {isolation}"
        )
    if vector_version != EXPECTED_VECTOR_VERSION:
        observed = vector_version or "not installed"
        raise PostgresCompatibilityError(
            f"pgvector 0.8.1 is required; connected extension is {observed}"
        )
    return PostgresCapabilities(
        server_version,
        version_num,
        vector_version,
        isolation,
    )


def main() -> int:
    """Check the fixed deployment contract without printing connection values."""

    try:
        config = PostgresConfig.from_env()
        capabilities = verify_postgres_capabilities(
            PostgresTransactionManager(partial(connect, config))
        )
    except (
        PostgresCompatibilityError,
        PostgresConfigurationError,
        PostgresDependencyError,
        ConnectionError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("PostgreSQL capability check failed", file=sys.stderr)
        return 1
    print(
        f"PostgreSQL {capabilities.server_version}; "
        f"pgvector {capabilities.vector_version}; "
        f"isolation {capabilities.transaction_isolation}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
