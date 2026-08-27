"""PostgreSQL storage for long-lived MCP token hashes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from src.infra.postgres.transaction import PostgresTransactionManager
from src.shared.mcp import McpTokenRecord


_TOKEN_COLUMNS = """
    token_id, owner_user_id, key_sha256, created_at, last_used_at, revoked_at
"""


def _value(row: object, index: int, name: str) -> object:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]  # type: ignore[index]


def _record(row: object | None) -> McpTokenRecord | None:
    if row is None:
        return None
    return McpTokenRecord(
        str(_value(row, 0, "token_id")),
        int(_value(row, 1, "owner_user_id")),
        str(_value(row, 2, "key_sha256")),
        _value(row, 3, "created_at"),  # type: ignore[arg-type]
        _value(row, 4, "last_used_at"),  # type: ignore[arg-type]
        _value(row, 5, "revoked_at"),  # type: ignore[arg-type]
    )


def _valid_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


class PostgresMcpTokenStore:
    def __init__(self, transactions: PostgresTransactionManager) -> None:
        self._transactions = transactions

    def insert(self, record: McpTokenRecord) -> None:
        self._execute(
            """
                INSERT INTO mcp_tokens (
                    token_id, owner_user_id, key_sha256,
                    created_at, last_used_at, revoked_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                record.token_id,
                record.owner_user_id,
                record.key_sha256,
                record.created_at,
                record.last_used_at,
                record.revoked_at,
            ),
        )

    def get(self, token_id: str) -> McpTokenRecord | None:
        if not _valid_uuid(token_id):
            return None
        return self._one(
            f"SELECT {_TOKEN_COLUMNS} FROM mcp_tokens WHERE token_id = %s",
            (token_id,),
        )

    def find_by_hash(self, key_sha256: str) -> McpTokenRecord | None:
        return self._one(
            f"SELECT {_TOKEN_COLUMNS} FROM mcp_tokens WHERE key_sha256 = %s",
            (key_sha256,),
        )

    def list_for_owner(self, owner_user_id: int) -> Sequence[McpTokenRecord]:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                        SELECT {_TOKEN_COLUMNS}
                        FROM mcp_tokens
                        WHERE owner_user_id = %s
                        ORDER BY created_at, token_id
                    """,
                    (owner_user_id,),
                )
                return tuple(_record(row) for row in cursor.fetchall())  # type: ignore[misc]
            finally:
                cursor.close()

    def update(self, record: McpTokenRecord) -> McpTokenRecord:
        """Preserve the first committed revocation timestamp across nodes."""

        if not _valid_uuid(record.token_id):
            raise KeyError(record.token_id)
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"""
                        UPDATE mcp_tokens
                        SET revoked_at = COALESCE(
                            revoked_at,
                            GREATEST(%s, last_used_at, created_at)
                        )
                        WHERE token_id = %s
                        RETURNING {_TOKEN_COLUMNS}
                    """,
                    (record.revoked_at, record.token_id),
                )
                stored = _record(cursor.fetchone())
                if stored is None:
                    raise KeyError(record.token_id)
                return stored
            finally:
                cursor.close()

    def touch_last_used_if_active(
        self, token_id: str, key_sha256: str, used_at: datetime
    ) -> bool:
        if not _valid_uuid(token_id):
            return False
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                        UPDATE mcp_tokens
                        SET last_used_at = GREATEST(
                            last_used_at,
                            created_at,
                            %s
                        )
                        WHERE token_id = %s
                          AND key_sha256 = %s
                          AND revoked_at IS NULL
                        RETURNING token_id
                    """,
                    (used_at, token_id, key_sha256),
                )
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def _one(
        self, sql: str, parameters: tuple[object, ...]
    ) -> McpTokenRecord | None:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, parameters)
                return _record(cursor.fetchone())
            finally:
                cursor.close()

    def _execute(self, sql: str, parameters: tuple[object, ...]) -> None:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, parameters)
            finally:
                cursor.close()
