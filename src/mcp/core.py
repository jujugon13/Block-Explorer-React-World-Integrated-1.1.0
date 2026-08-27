"""HTTP and JSON-RPC boundary for VectorShelf external tools."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime

from src.shared import (
    McpTokenStore,
    McpToolBackend,
    Principal,
    PublicError,
    Request,
    Response,
)

from .tokens import McpTokenService
from .tools import McpToolService, TOOL_DEFINITIONS, ToolRateLimiter


def _json_object(raw: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicError("COMMON-002") from error
    if not isinstance(value, Mapping):
        raise PublicError("COMMON-002")
    return value


def _protocol_response(request_id: object, result: Mapping[str, object]) -> Response:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "result": result},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return Response(
        200,
        body,
        (("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))),
    )


class McpService:
    def __init__(
        self,
        backend: McpToolBackend,
        *,
        token_store: McpTokenStore,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        random_bytes: Callable[[int], bytes] | None = None,
        token_id: Callable[[], str] | None = None,
        principal_factory: Callable[[int], Principal | None] | None = None,
    ) -> None:
        token_options: dict[str, object] = {}
        if random_bytes is not None:
            token_options["random_bytes"] = random_bytes
        if token_id is not None:
            token_options["token_id"] = token_id
        self.tokens = McpTokenService(
            token_store,
            clock=clock,
            principal_factory=principal_factory,
            **token_options,
        )
        self.tools = McpToolService(
            backend, rate_limiter=ToolRateLimiter(monotonic)
        )

    def mount(self, app: object) -> None:
        add_route = getattr(app, "add_route")
        add_route("POST", "/mcp/tokens", self.issue_token, success_status=201)
        add_route("GET", "/mcp/tokens", self.list_tokens)
        add_route("DELETE", "/mcp/tokens/{tokenId}", self.revoke_token)
        add_route("POST", "/mcp", self.protocol)

    def issue_token(self, request: Request) -> dict[str, object]:
        return self.tokens.issue(request.principal)

    def list_tokens(self, request: Request) -> list[dict[str, object]]:
        return self.tokens.list(request.principal)

    def revoke_token(self, request: Request) -> dict[str, object]:
        token_id = request.path_params.get("tokenId")
        if not token_id:
            raise PublicError("COMMON-002")
        return self.tokens.revoke(request.principal, token_id)

    def authenticated_request(self, request: Request) -> Request:
        principal = self.tokens.authenticate_header(request.header("authorization"))
        if principal is None:
            raise PublicError("COMMON-007")
        return replace(request, principal=principal)

    def protocol(self, request: Request) -> Response:
        """Authenticate once and preserve that principal for redispatch-safe work."""
        return self._protocol_for(self.authenticated_request(request))

    def _protocol_for(self, request: Request) -> Response:
        principal = request.principal
        if principal is None:
            raise PublicError("COMMON-007")
        payload = _json_object(request.body)
        if payload.get("jsonrpc") != "2.0":
            raise PublicError("COMMON-002")
        request_id = payload.get("id")
        method = payload.get("method")
        if method == "tools/list":
            return _protocol_response(request_id, {"tools": list(TOOL_DEFINITIONS)})
        if method != "tools/call":
            raise PublicError("COMMON-002")
        params = payload.get("params")
        if not isinstance(params, Mapping) or not isinstance(params.get("name"), str):
            raise PublicError("COMMON-002")
        arguments = params.get("arguments", {})
        value = self.tools.call(str(params["name"]), arguments, principal)
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return _protocol_response(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )
