from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime, timedelta

from src.infra.http.testing import request_over_uvicorn
from src.mcp import (
    MCP_PRINCIPAL_SUBJECT,
    InMemoryMcpTokenStore,
    McpService,
    McpTokenService,
)
from src.platform import PlatformApp
from src.shared import McpDocumentAccess, Principal, PublicError, Request


P1 = Principal("one@example.com", frozenset({"USER"}), 1, 10, "One")
P2 = Principal("two@example.com", frozenset({"USER"}), 2, 20, "Two")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
        self.seconds = 0.0

    def wall(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.seconds += seconds


class Entropy:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, size: int) -> bytes:
        self.value += 1
        return bytes([self.value]) * size


class Backend:
    def __init__(self) -> None:
        self.results: list[dict[str, object]] = [
            {"documentId": 7, "content": "result", "optional": None}
        ]
        self.allowed = True
        self.deleted: set[int] = set()
        self.fail_search = False
        self.calls: list[tuple[object, ...]] = []

    def search_documents(self, principal, query, top_k):
        self.calls.append(("search", principal, query, top_k))
        if self.fail_search:
            raise RuntimeError("private backend detail")
        return self.results

    def document_access(self, principal, document_id):
        self.calls.append(("permission", principal, document_id))
        if not self.allowed:
            return McpDocumentAccess.DENIED
        if document_id in self.deleted:
            return McpDocumentAccess.DELETED
        return McpDocumentAccess.ALLOWED

    def get_document_detail(self, principal, document_id):
        self.calls.append(("detail", principal, document_id))
        if document_id in self.deleted:
            raise PublicError("DOCUMENT-001")
        return {"documentId": document_id, "description": None, "title": "Doc"}

    def get_indexing_status(self, principal, document_id):
        self.calls.append(("status", principal, document_id))
        return {"documentId": document_id, "processingVersion": None}


class RevokingMemoryStore(InMemoryMcpTokenStore):
    def __init__(self) -> None:
        super().__init__()
        self.before_touch = None

    def touch_last_used_if_active(self, token_id, key_sha256, used_at):
        if self.before_touch is not None:
            callback, self.before_touch = self.before_touch, None
            callback()
        return super().touch_last_used_if_active(token_id, key_sha256, used_at)


def request(
    app: PlatformApp,
    method: str,
    path: str,
    *,
    bearer: str | None = None,
    payload: object | None = None,
) -> tuple[int, object | None]:
    body = b"" if payload is None else json.dumps(payload).encode()
    headers = {}
    if body:
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    response = request_over_uvicorn(
        app.handle, Request(method, path, headers, body)
    )
    return response.status, json.loads(response.body) if response.body else None


class McpAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.store = InMemoryMcpTokenStore()
        self.backend = Backend()
        self.ids = iter(f"token-{number}" for number in range(1, 100))
        self.service = McpService(
            self.backend,
            token_store=self.store,
            clock=self.clock.wall,
            monotonic=self.clock.monotonic,
            random_bytes=Entropy(),
            token_id=lambda: next(self.ids),
            principal_factory=lambda user_id: {1: P1, 2: P2}.get(user_id),
        )

        def web_principal(req):
            return {"Bearer jwt-one": P1, "Bearer jwt-two": P2}.get(
                req.header("authorization")
            )

        self.app = PlatformApp(web_principal, self.clock.wall)
        self.service.mount(self.app)

    def issue(self, jwt: str = "jwt-one") -> dict[str, object]:
        status, body = request(self.app, "POST", "/mcp/tokens", bearer=jwt)
        self.assertEqual(201, status)
        return body["data"]

    def tool(
        self, raw_key: str, name: str, arguments: object
    ) -> tuple[int, object | None]:
        return request(
            self.app,
            "POST",
            "/mcp",
            bearer=raw_key,
            payload={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )

    @staticmethod
    def tool_value(body: object) -> object:
        return json.loads(body["result"]["content"][0]["text"])

    def test_AC_MCP_001_issue_returns_one_time_prefixed_raw_key_and_stores_hash_only(self):
        issued = self.issue()
        raw_key = issued["apiKey"]

        self.assertTrue(raw_key.startswith("vectorshelf_mcp_"))
        self.assertEqual(59, len(raw_key))
        self.assertNotIn("=", raw_key)
        self.assertEqual(1, len(self.store.records))
        record = self.store.records[0]
        self.assertEqual(hashlib.sha256(raw_key.encode()).hexdigest(), record.key_sha256)
        self.assertNotIn(raw_key, repr(record))

    def test_AC_MCP_002_list_never_contains_raw_key(self):
        issued = self.issue()
        status, body = request(self.app, "GET", "/mcp/tokens", bearer="jwt-one")

        self.assertEqual(200, status)
        self.assertEqual(1, len(body["data"]))
        self.assertNotIn("apiKey", body["data"][0])
        self.assertNotIn(issued["apiKey"], json.dumps(body))
        self.assertIn("lastUsedAt", body["data"][0])

    def test_AC_MCP_003_repeated_revocation_keeps_original_timestamp(self):
        issued = self.issue()
        path = f"/mcp/tokens/{issued['tokenId']}"
        first_status, first = request(self.app, "DELETE", path, bearer="jwt-one")
        self.clock.advance(30)
        again_status, again = request(self.app, "DELETE", path, bearer="jwt-one")

        self.assertEqual((200, 200), (first_status, again_status))
        self.assertEqual(first["data"]["revokedAt"], again["data"]["revokedAt"])

    def test_AC_MCP_004_owner_boundary_rejects_another_users_revocation(self):
        issued = self.issue()
        status, body = request(
            self.app,
            "DELETE",
            f"/mcp/tokens/{issued['tokenId']}",
            bearer="jwt-two",
        )

        self.assertEqual(403, status)
        self.assertEqual("ROLE-002", body["code"])

    def test_AC_MCP_005_revoked_key_cannot_reach_protocol_tools(self):
        issued = self.issue()
        request(
            self.app,
            "DELETE",
            f"/mcp/tokens/{issued['tokenId']}",
            bearer="jwt-one",
        )
        status, body = self.tool(
            issued["apiKey"], "search_documents", {"query": "query"}
        )

        self.assertEqual(401, status)
        self.assertEqual("COMMON-007", body["code"])
        self.assertEqual([], self.backend.calls)

    def test_AC_MCP_005_successful_authentication_updates_last_used_time(self):
        issued = self.issue()
        self.assertIsNone(self.store.records[0].last_used_at)
        self.clock.advance(3)
        status, _ = request(
            self.app,
            "POST",
            "/mcp",
            bearer=issued["apiKey"],
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(200, status)
        self.assertEqual(self.clock.now, self.store.records[0].last_used_at)

    def test_AC_MCP_005_concurrent_revocation_cannot_be_lost_by_authentication(self):
        store = RevokingMemoryStore()
        entropy = Entropy()
        first = McpTokenService(
            store,
            clock=self.clock.wall,
            random_bytes=entropy,
            token_id=lambda: "racing-token",
            principal_factory=lambda user_id: P1,
        )
        second = McpTokenService(
            store,
            clock=self.clock.wall,
            principal_factory=lambda user_id: P1,
        )
        issued = first.issue(P1)
        store.before_touch = lambda: second.revoke(P1, "racing-token")

        self.assertIsNone(first.authenticate(issued["apiKey"]))
        self.assertIsNotNone(store.get("racing-token").revoked_at)

    def test_AC_MCP_006_query_length_2001_is_COMMON_002(self):
        raw_key = self.issue()["apiKey"]
        status, body = self.tool(
            raw_key, "search_documents", {"query": "가" * 2_001}
        )

        self.assertEqual(400, status)
        self.assertEqual("COMMON-002", body["code"])

    def test_AC_MCP_006_blank_bounds_and_default_top_k_are_validated(self):
        raw_key = self.issue()["apiKey"]
        for arguments in (
            {"query": ""},
            {"query": "   "},
            {"query": "q", "topK": 0},
            {"query": "q", "topK": 21},
            {"query": "q", "topK": True},
        ):
            with self.subTest(arguments=arguments):
                status, body = self.tool(raw_key, "search_documents", arguments)
                self.assertEqual((400, "COMMON-002"), (status, body["code"]))
        status, _ = self.tool(raw_key, "search_documents", {"query": "q"})
        self.assertEqual(200, status)
        self.assertEqual(5, self.backend.calls[-1][3])

    def test_AC_MCP_007_twenty_first_search_in_window_is_MCP_001(self):
        raw_key = self.issue()["apiKey"]
        for number in range(20):
            status, _ = self.tool(
                raw_key, "search_documents", {"query": f"query {number}"}
            )
            self.assertEqual(200, status)

        status, body = self.tool(raw_key, "search_documents", {"query": "blocked"})
        self.assertEqual(429, status)
        self.assertEqual("MCP-001", body["code"])
        self.assertEqual("도구 호출 한도를 초과했습니다.", body["message"])
        self.clock.advance(60)
        status, _ = self.tool(raw_key, "search_documents", {"query": "new window"})
        self.assertEqual(200, status)

    def test_AC_MCP_008_document_limit_is_independent_from_search_limit(self):
        raw_key = self.issue()["apiKey"]
        for number in range(20):
            self.tool(raw_key, "search_documents", {"query": f"query {number}"})
        status, body = self.tool(
            raw_key, "get_document_detail", {"documentId": 7}
        )

        self.assertEqual(200, status)
        self.assertEqual(7, self.tool_value(body)["documentId"])
        principal = next(call[1] for call in self.backend.calls if call[0] == "detail")
        self.assertEqual((MCP_PRINCIPAL_SUBJECT, 1), (principal.subject, principal.user_id))

    def test_AC_MCP_009_chunk_text_is_cut_at_1000_without_ellipsis(self):
        raw_key = self.issue()["apiKey"]
        self.backend.results = [{"documentId": 7, "content": "x" * 1_500}]
        status, body = self.tool(
            raw_key, "search_documents", {"query": "query", "topK": 5}
        )

        content = self.tool_value(body)[0]["content"]
        self.assertEqual(200, status)
        self.assertEqual("x" * 1_000, content)
        self.assertFalse(content.endswith("..."))

    def test_AC_MCP_010_permission_is_checked_before_document_lookup(self):
        raw_key = self.issue()["apiKey"]
        self.backend.allowed = False
        status, body = self.tool(
            raw_key, "get_document_detail", {"documentId": 404_404}
        )

        self.assertEqual(403, status)
        self.assertEqual("ROLE-002", body["code"])
        self.assertEqual(["permission"], [call[0] for call in self.backend.calls])

    def test_AC_MCP_011_deleted_document_is_reported_as_not_found(self):
        raw_key = self.issue()["apiKey"]
        self.backend.deleted.add(7)
        status, body = self.tool(
            raw_key, "get_document_detail", {"documentId": 7}
        )

        self.assertEqual(404, status)
        self.assertEqual("DOCUMENT-001", body["code"])
        self.assertEqual(["permission"], [call[0] for call in self.backend.calls])

    def test_AC_MCP_001_tool_list_declares_read_only_non_destructive_hints(self):
        raw_key = self.issue()["apiKey"]
        status, body = request(
            self.app,
            "POST",
            "/mcp",
            bearer=raw_key,
            payload={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        )

        self.assertEqual(200, status)
        self.assertEqual(3, len(body["result"]["tools"]))
        for tool in body["result"]["tools"]:
            self.assertEqual(
                {"readOnlyHint": True, "destructiveHint": False}, tool["annotations"]
            )

    def test_AC_MCP_002_tool_json_omits_null_fields_but_rest_token_json_keeps_them(self):
        raw_key = self.issue()["apiKey"]
        status, body = self.tool(
            raw_key, "get_document_detail", {"documentId": 7}
        )

        self.assertEqual(200, status)
        self.assertNotIn("description", self.tool_value(body))
        list_status, listed = request(
            self.app, "GET", "/mcp/tokens", bearer="jwt-one"
        )
        self.assertEqual(200, list_status)
        self.assertIn("revokedAt", listed["data"][0])

    def test_AC_MCP_006_unexpected_backend_error_maps_to_generic_server_error(self):
        raw_key = self.issue()["apiKey"]
        self.backend.fail_search = True
        status, body = self.tool(raw_key, "search_documents", {"query": "query"})

        self.assertEqual(500, status)
        self.assertEqual("COMMON-006", body["code"])
        self.assertNotIn("private backend detail", json.dumps(body))
