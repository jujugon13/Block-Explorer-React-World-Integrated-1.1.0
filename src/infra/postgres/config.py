"""Environment-only PostgreSQL connection configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


ENVIRONMENT_KEYS = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)

# A Multi-AZ failover blackholes the former primary's address, so a socket that
# is already waiting on a reply never fails on its own; without keepalives the
# blocked thread holds its claim and keeps renewing the lease, which stops the
# expired-lease recovery in RecoveryMixin from ever seeing the job.  Detection
# must therefore finish inside the worker dead threshold (30s by default).
# Windows ignores keepalives_count and retries a platform-chosen number of
# times, so the idle and interval values are kept small enough that even ten
# retries stay under that threshold; Linux additionally honours
# tcp_user_timeout.  No statement_timeout is set here: migrations share this
# connection and 0009 builds an HNSW index whose duration grows with the table.
CONNECT_TIMEOUT_SECONDS = 5
KEEPALIVE_IDLE_SECONDS = 5
KEEPALIVE_INTERVAL_SECONDS = 2
KEEPALIVE_COUNT = 3
TCP_USER_TIMEOUT_MILLISECONDS = 15_000


class PostgresConfigurationError(ValueError):
    """The required PostgreSQL environment is absent or malformed."""


class PostgresDependencyError(RuntimeError):
    """The configured PostgreSQL driver is unavailable."""


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """Connection values whose representation never exposes their contents."""

    host: str = field(repr=False)
    port: int = field(repr=False)
    dbname: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def from_env(
        cls, environment: Mapping[str, str] | None = None
    ) -> "PostgresConfig":
        source = os.environ if environment is None else environment
        values = {key: source.get(key) for key in ENVIRONMENT_KEYS}
        missing = tuple(
            key
            for key in ENVIRONMENT_KEYS
            if values[key] is None or values[key] == ""
        )
        if missing:
            raise PostgresConfigurationError(
                "missing PostgreSQL environment variables: " + ", ".join(missing)
            )

        port_text = values["DB_PORT"]
        if port_text is None or not port_text.isascii() or not port_text.isdecimal():
            raise PostgresConfigurationError("DB_PORT must be an integer from 1 to 65535")
        port = int(port_text)
        if not 1 <= port <= 65_535:
            raise PostgresConfigurationError("DB_PORT must be an integer from 1 to 65535")

        for key in ("DB_HOST", "DB_NAME", "DB_USER"):
            value = values[key]
            if value is None or not value.strip():
                raise PostgresConfigurationError(f"{key} must not be blank")

        return cls(
            host=str(values["DB_HOST"]),
            port=port,
            dbname=str(values["DB_NAME"]),
            user=str(values["DB_USER"]),
            password=str(values["DB_PASSWORD"]),
        )

    def connect_kwargs(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "keepalives": 1,
            "keepalives_idle": KEEPALIVE_IDLE_SECONDS,
            "keepalives_interval": KEEPALIVE_INTERVAL_SECONDS,
            "keepalives_count": KEEPALIVE_COUNT,
            "tcp_user_timeout": TCP_USER_TIMEOUT_MILLISECONDS,
        }

    def __repr__(self) -> str:
        return "PostgresConfig(<redacted>)"


def connect(config: PostgresConfig):
    """Open a psycopg connection without importing the driver at module load."""

    try:
        import psycopg
    except ImportError:
        raise PostgresDependencyError(
            "psycopg 3 is required for PostgreSQL connections"
        ) from None
    return psycopg.connect(**config.connect_kwargs())
