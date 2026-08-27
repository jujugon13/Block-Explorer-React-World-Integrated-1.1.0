"""Small SockJS XHR transport that feeds the shared STOMP frame processor."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from threading import RLock

from src.shared import PublicError, Request, Response

from .core import origin_allowed
from .websocket import (
    StompConnectionRejected,
    StompFrame,
    StompFrameProcessor,
    StompFrameRejected,
    StompHandshakeRejected,
)


_SERVER = re.compile(r"^[0-9]{1,3}$")
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_XHR_HEADERS = (
    ("Content-Type", "application/javascript; charset=UTF-8"),
    ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
)


@dataclass(slots=True)
class _SockJsSession:
    stomp_id: str
    opened: bool = False
    outbound: deque[bytes] = field(default_factory=deque)
    lock: RLock = field(default_factory=RLock)


class SockJsHttpTransport:
    """Expose one standards-compatible XHR fallback below the `/ws` endpoint."""

    def __init__(self, processor: StompFrameProcessor) -> None:
        self.processor = processor
        self._sessions: dict[tuple[str, str], _SockJsSession] = {}
        self._lock = RLock()

    def mount(self, app: object) -> None:
        add_route = getattr(app, "add_route")
        add_route("GET", "/ws", self.welcome)
        add_route("GET", "/ws/info", self.info)
        add_route("POST", "/ws/{server}/{session}/xhr", self.receive)
        add_route("POST", "/ws/{server}/{session}/xhr_send", self.send)
        add_route("GET", "/ws/{server}/{session}/websocket", self.upgrade_required)

    @staticmethod
    def _origin(request: Request) -> str | None:
        origin = request.header("origin")
        if origin is not None and not origin_allowed(origin):
            raise PublicError("ROLE-002")
        return origin

    @staticmethod
    def _key(request: Request) -> tuple[str, str]:
        server = request.path_params.get("server", "")
        session = request.path_params.get("session", "")
        if not _SERVER.fullmatch(server) or not _SESSION.fullmatch(session):
            raise PublicError("COMMON-002")
        return server, session

    def _get_or_create(self, request: Request) -> _SockJsSession:
        key = self._key(request)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            pending: deque[bytes] = deque()
            try:
                stomp_id = self.processor.handshake(
                    self._origin(request), lambda frame: pending.append(frame.encode())
                )
            except StompHandshakeRejected as error:
                raise PublicError("ROLE-002") from error
            created = _SockJsSession(stomp_id, outbound=pending)
            self._sessions[key] = created
            return created

    def _existing(self, request: Request) -> _SockJsSession:
        key = self._key(request)
        with self._lock:
            session = self._sessions.get(key)
        if session is None:
            raise PublicError("COMMON-003")
        return session

    @staticmethod
    def welcome(request: Request) -> Response:
        SockJsHttpTransport._origin(request)
        return Response(200, b"Welcome to SockJS!\n", (("Content-Type", "text/plain; charset=UTF-8"),))

    @staticmethod
    def info(request: Request) -> Response:
        SockJsHttpTransport._origin(request)
        body = json.dumps(
            {"websocket": True, "cookie_needed": False, "origins": ["*:*"], "entropy": 0},
            separators=(",", ":"),
        ).encode("utf-8")
        return Response(
            200,
            body,
            (("Content-Type", "application/json; charset=UTF-8"), ("Cache-Control", "no-store")),
        )

    def receive(self, request: Request) -> Response:
        session = self._get_or_create(request)
        with session.lock:
            if not session.opened:
                session.opened = True
                return Response(200, b"o\n", _XHR_HEADERS)
            if not session.outbound:
                return Response(200, b"h\n", _XHR_HEADERS)
            frames = [item.decode("utf-8") for item in session.outbound]
            session.outbound.clear()
        body = ("a" + json.dumps(frames, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        return Response(200, body, _XHR_HEADERS)

    def send(self, request: Request) -> Response:
        session = self._existing(request)
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PublicError("COMMON-002") from None
        if (
            not isinstance(payload, list)
            or not payload
            or any(not isinstance(item, str) or not item for item in payload)
        ):
            raise PublicError("COMMON-002")
        try:
            for raw in payload:
                response = self.processor.process_raw(
                    session.stomp_id, raw.encode("utf-8")
                )
                if response is not None:
                    with session.lock:
                        session.outbound.append(response)
        except (StompConnectionRejected, StompFrameRejected) as error:
            with session.lock:
                session.outbound.append(
                    StompFrame("ERROR", {"message": str(error)}).encode()
                )
        return Response(204)

    @staticmethod
    def upgrade_required(request: Request) -> Response:
        SockJsHttpTransport._origin(request)
        return Response(
            426,
            b"WebSocket upgrade must be handled by the hosting server.",
            (("Upgrade", "websocket"), ("Content-Type", "text/plain; charset=UTF-8")),
        )
