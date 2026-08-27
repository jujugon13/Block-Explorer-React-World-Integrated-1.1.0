"""Durable stdlib SQLite adapter for MCP API-key hashes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from threading import RLock

from src.shared import McpTokenRecord, open_sqlite_database


class SqliteMcpTokenStore:
    """Persist token hashes and revocation state across process restarts."""

    def __init__(self, database: str | Path) -> None:
        self._connection = open_sqlite_database(database)
        self._lock = RLock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tokens (
                    token_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    key_sha256 TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                )
                """
            )

    @staticmethod
    def _record(row: Mapping[str, object] | None) -> McpTokenRecord | None:
        if row is None:
            return None
        return McpTokenRecord(
            str(row["token_id"]),
            int(row["owner_user_id"]),
            str(row["key_sha256"]),
            datetime.fromisoformat(str(row["created_at"])),
            (
                datetime.fromisoformat(str(row["last_used_at"]))
                if row["last_used_at"] is not None
                else None
            ),
            (
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
        )

    def insert(self, record: McpTokenRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO mcp_tokens
                    (token_id, owner_user_id, key_sha256, created_at, last_used_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.token_id,
                    record.owner_user_id,
                    record.key_sha256,
                    record.created_at.isoformat(),
                    record.last_used_at.isoformat() if record.last_used_at else None,
                    record.revoked_at.isoformat() if record.revoked_at else None,
                ),
            )

    def get(self, token_id: str) -> McpTokenRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM mcp_tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            return self._record(row)

    def find_by_hash(self, key_sha256: str) -> McpTokenRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM mcp_tokens WHERE key_sha256 = ?", (key_sha256,)
            ).fetchone()
            return self._record(row)

    def list_for_owner(self, owner_user_id: int) -> Sequence[McpTokenRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM mcp_tokens
                WHERE owner_user_id = ?
                ORDER BY created_at, token_id
                """,
                (owner_user_id,),
            ).fetchall()
            return tuple(self._record(row) for row in rows)  # type: ignore[misc]

    def update(self, record: McpTokenRecord) -> McpTokenRecord:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE mcp_tokens
                SET owner_user_id = ?, key_sha256 = ?, created_at = ?,
                    last_used_at = ?, revoked_at = ?
                WHERE token_id = ?
                """,
                (
                    record.owner_user_id,
                    record.key_sha256,
                    record.created_at.isoformat(),
                    record.last_used_at.isoformat() if record.last_used_at else None,
                    record.revoked_at.isoformat() if record.revoked_at else None,
                    record.token_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(record.token_id)
            return record

    def touch_last_used_if_active(
        self, token_id: str, key_sha256: str, used_at: datetime
    ) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE mcp_tokens
                SET last_used_at = ?
                WHERE token_id = ? AND key_sha256 = ? AND revoked_at IS NULL
                """,
                (used_at.isoformat(), token_id, key_sha256),
            )
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._connection.close()
