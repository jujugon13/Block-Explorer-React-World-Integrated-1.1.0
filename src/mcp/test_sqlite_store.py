from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from src.mcp import McpTokenService, SqliteMcpTokenStore
from src.shared import Principal


NOW = datetime(2026, 8, 27, tzinfo=UTC)
OWNER = Principal("owner", frozenset({"USER"}), user_id=7)


class RevokingSqliteStore(SqliteMcpTokenStore):
    def __init__(self, database) -> None:
        super().__init__(database)
        self.before_touch = None

    def touch_last_used_if_active(self, token_id, key_sha256, used_at):
        if self.before_touch is not None:
            callback, self.before_touch = self.before_touch, None
            callback()
        return super().touch_last_used_if_active(token_id, key_sha256, used_at)


class McpSqliteAcceptanceTests(unittest.TestCase):
    def test_AC_MCP_001_hash_record_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.sqlite3"
            first = SqliteMcpTokenStore(path)
            issued = McpTokenService(
                first,
                clock=lambda: NOW,
                random_bytes=lambda size: b"x" * size,
                token_id=lambda: "token-1",
                principal_factory=lambda user_id: OWNER,
            ).issue(OWNER)
            first.close()

            reopened = SqliteMcpTokenStore(path)
            service = McpTokenService(
                reopened,
                clock=lambda: NOW,
                principal_factory=lambda user_id: OWNER,
            )
            self.assertEqual(OWNER.user_id, service.authenticate(issued["apiKey"]).user_id)
            self.assertNotIn(issued["apiKey"], repr(reopened.get("token-1")))
            reopened.close()

    def test_AC_MCP_005_revocation_survives_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.sqlite3"
            first = SqliteMcpTokenStore(path)
            service = McpTokenService(
                first,
                clock=lambda: NOW,
                random_bytes=lambda size: b"y" * size,
                token_id=lambda: "token-2",
                principal_factory=lambda user_id: OWNER,
            )
            issued = service.issue(OWNER)
            service.revoke(OWNER, "token-2")
            first.close()

            reopened = SqliteMcpTokenStore(path)
            service = McpTokenService(
                reopened,
                clock=lambda: NOW,
                principal_factory=lambda user_id: OWNER,
            )
            self.assertIsNone(service.authenticate(issued["apiKey"]))
            reopened.close()

    def test_AC_MCP_005_shared_sqlite_store_revocation_wins_authentication_race(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.sqlite3"
            store = RevokingSqliteStore(path)
            first = McpTokenService(
                store,
                clock=lambda: NOW,
                random_bytes=lambda size: b"z" * size,
                token_id=lambda: "token-3",
                principal_factory=lambda user_id: OWNER,
            )
            second = McpTokenService(
                store,
                clock=lambda: NOW,
                principal_factory=lambda user_id: OWNER,
            )
            issued = first.issue(OWNER)
            store.before_touch = lambda: second.revoke(OWNER, "token-3")

            self.assertIsNone(first.authenticate(issued["apiKey"]))
            self.assertIsNotNone(store.get("token-3").revoked_at)
            store.close()


if __name__ == "__main__":
    unittest.main()
