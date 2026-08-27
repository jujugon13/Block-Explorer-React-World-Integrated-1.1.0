"""STOMP frame authentication, authorization, and in-memory delivery."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from secrets import token_hex
from threading import RLock
from typing import Protocol

from src.shared import (
    Principal,
    PrincipalResolver,
    Request,
    WebSocketResult,
    WebSocketSink,
)

from .core import origin_allowed


FrameSink = Callable[["StompFrame"], None]


class StompRejected(Exception):
    """A server adapter must reject the current STOMP operation."""


class StompHandshakeRejected(StompRejected):
    """The HTTP upgrade origin is not allowed."""


class StompConnectionRejected(StompRejected):
    """The CONNECT frame failed authentication."""


class StompFrameRejected(StompRejected):
    """An authenticated destination operation is not allowed."""


@dataclass(frozen=True, slots=True)
class StompFrame:
    """A minimal STOMP 1.2 frame value suitable for a network adapter."""

    command: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", self.command.upper())
        object.__setattr__(self, "headers", dict(self.headers))

    @classmethod
    def parse(cls, raw: bytes) -> "StompFrame":
        if not raw.endswith(b"\x00"):
            raise StompFrameRejected("unterminated STOMP frame")
        head, separator, body = raw[:-1].partition(b"\n\n")
        if not separator:
            head, separator, body = raw[:-1].partition(b"\r\n\r\n")
        if not separator:
            raise StompFrameRejected("missing STOMP header terminator")
        try:
            lines = head.replace(b"\r\n", b"\n").decode("utf-8").split("\n")
        except UnicodeDecodeError as error:
            raise StompFrameRejected("invalid STOMP header encoding") from error
        if not lines or not lines[0]:
            raise StompFrameRejected("missing STOMP command")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                raise StompFrameRejected("invalid STOMP header")
            name, value = line.split(":", 1)
            if not name:
                raise StompFrameRejected("invalid STOMP header")
            headers.setdefault(name, value)
        length = headers.get("content-length")
        if length is not None:
            try:
                expected = int(length)
            except ValueError as error:
                raise StompFrameRejected("invalid STOMP content-length") from error
            if expected < 0 or len(body) != expected:
                raise StompFrameRejected("invalid STOMP content-length")
        return cls(lines[0], headers, body)

    def encode(self) -> bytes:
        lines = [self.command]
        lines.extend(f"{name}:{value}" for name, value in self.headers.items())
        return ("\n".join(lines) + "\n\n").encode("utf-8") + self.body + b"\x00"


class DestinationPolicy(Protocol):
    def can_subscribe(self, destination: str, principal: Principal | None) -> bool: ...

    def can_send(self, destination: str, principal: Principal | None) -> bool: ...


@dataclass(slots=True)
class _Subscription:
    session_id: str
    subscription_id: str
    sink: FrameSink


class InMemoryStompBroker:
    """Volatile fan-out broker: disconnected clients get no replay."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, dict[tuple[str, str], _Subscription]] = {}
        self._message_id = 0
        self._lock = RLock()

    def subscribe(
        self,
        session_id: str,
        subscription_id: str,
        destination: str,
        sink: FrameSink,
    ) -> None:
        with self._lock:
            self._subscriptions.setdefault(destination, {})[
                (session_id, subscription_id)
            ] = _Subscription(session_id, subscription_id, sink)

    def unsubscribe(self, session_id: str, subscription_id: str) -> None:
        with self._lock:
            for subscriptions in self._subscriptions.values():
                subscriptions.pop((session_id, subscription_id), None)

    def disconnect(self, session_id: str) -> None:
        with self._lock:
            for subscriptions in self._subscriptions.values():
                for key in tuple(subscriptions):
                    if key[0] == session_id:
                        subscriptions.pop(key, None)

    def publish(self, destination: str, body: bytes, content_type: str) -> int:
        with self._lock:
            self._message_id += 1
            message_id = str(self._message_id)
            subscriptions = tuple(self._subscriptions.get(destination, {}).values())
        delivered = 0
        failed: list[tuple[str, str]] = []
        for subscription in subscriptions:
            frame = StompFrame(
                "MESSAGE",
                {
                    "destination": destination,
                    "subscription": subscription.subscription_id,
                    "message-id": message_id,
                    "content-type": content_type,
                    "content-length": str(len(body)),
                },
                body,
            )
            try:
                subscription.sink(frame)
                delivered += 1
            except Exception:
                failed.append((subscription.session_id, subscription.subscription_id))
        for session_id, subscription_id in failed:
            self.unsubscribe(session_id, subscription_id)
        return delivered

    def subscriber_count(self, destination: str) -> int:
        with self._lock:
            return len(self._subscriptions.get(destination, ()))


@dataclass(slots=True)
class _Session:
    sink: FrameSink
    authorization: str | None = None
    principal: Principal | None = None
    connected: bool = False


class StompFrameProcessor:
    """Performs open HTTP handshakes and secured STOMP destination handling."""

    def __init__(
        self,
        principal_resolver: PrincipalResolver,
        destination_policy: DestinationPolicy,
        broker: InMemoryStompBroker,
        *,
        session_id: Callable[[], str] = lambda: token_hex(16),
    ) -> None:
        self._principal_resolver = principal_resolver
        self._destination_policy = destination_policy
        self._broker = broker
        self._session_id = session_id
        self._sessions: dict[str, _Session] = {}
        self._lock = RLock()

    def handshake(self, origin: str | None, sink: FrameSink) -> str:
        """Open the HTTP transport without requiring an Authorization header."""

        if origin is not None and not origin_allowed(origin):
            raise StompHandshakeRejected("origin is not allowed")
        session_id = self._session_id()
        with self._lock:
            if session_id in self._sessions:
                raise RuntimeError("duplicate STOMP session id")
            self._sessions[session_id] = _Session(sink)
        return session_id

    def process_raw(self, session_id: str, raw: bytes) -> bytes | None:
        response = self.process(session_id, StompFrame.parse(raw))
        return response.encode() if response is not None else None

    def process(self, session_id: str, frame: StompFrame) -> StompFrame | None:
        session = self._session(session_id)
        if frame.command == "CONNECT":
            return self._connect(session_id, session, frame)
        if not session.connected:
            self._reject_connection(session_id, "CONNECT is required")
        if frame.command == "SUBSCRIBE":
            self._subscribe(session_id, session, frame)
            return None
        if frame.command == "UNSUBSCRIBE":
            subscription_id = frame.headers.get("id")
            if not subscription_id:
                raise StompFrameRejected("missing subscription id")
            self._broker.unsubscribe(session_id, subscription_id)
            return None
        if frame.command == "SEND":
            destination = frame.headers.get("destination", "")
            if not self._destination_policy.can_send(destination, session.principal):
                raise StompFrameRejected("destination send is not allowed")
            raise StompFrameRejected("destination send is unsupported")
        if frame.command == "DISCONNECT":
            self.disconnect(session_id)
            return None
        raise StompFrameRejected("unsupported STOMP command")

    def disconnect(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
        self._broker.disconnect(session_id)

    def _session(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise StompConnectionRejected("unknown STOMP session")
        return session

    def _connect(
        self, session_id: str, session: _Session, frame: StompFrame
    ) -> StompFrame:
        if session.connected:
            self._reject_connection(session_id, "duplicate CONNECT frame")
        authorization = frame.headers.get("Authorization")
        if (
            authorization is None
            or not authorization.startswith("Bearer ")
            or not authorization[7:]
        ):
            self._reject_connection(session_id, "missing bearer token")
        principal = self._resolve(authorization)
        if principal is None:
            self._reject_connection(session_id, "invalid bearer token")
        session.authorization = authorization
        session.principal = principal
        session.connected = True
        return StompFrame("CONNECTED", {"version": "1.2"})

    def _subscribe(self, session_id: str, session: _Session, frame: StompFrame) -> None:
        destination = frame.headers.get("destination")
        subscription_id = frame.headers.get("id")
        if not destination or not subscription_id:
            raise StompFrameRejected("missing subscription destination or id")
        principal = self._resolve(session.authorization)
        if principal is None:
            self._reject_connection(session_id, "expired bearer token")
        session.principal = principal
        if not self._destination_policy.can_subscribe(destination, principal):
            raise StompFrameRejected("destination subscription is not allowed")
        self._broker.subscribe(session_id, subscription_id, destination, session.sink)

    def _resolve(self, authorization: str | None) -> Principal | None:
        if authorization is None:
            return None
        try:
            return self._principal_resolver(
                Request("CONNECT", "/ws", {"Authorization": authorization})
            )
        except Exception:
            return None

    def _reject_connection(self, session_id: str, reason: str) -> None:
        self.disconnect(session_id)
        raise StompConnectionRejected(reason)


class StompWebSocketGateway:
    """Translate native WebSocket bytes to the existing STOMP processor."""

    def __init__(self, processor: StompFrameProcessor) -> None:
        self.processor = processor

    def open(self, origin: str | None, sink: WebSocketSink) -> str | None:
        try:
            return self.processor.handshake(
                origin, lambda frame: sink(frame.encode())
            )
        except StompHandshakeRejected:
            return None

    def receive(self, session_id: str, payload: bytes) -> WebSocketResult:
        try:
            return WebSocketResult(self.processor.process_raw(session_id, payload))
        except StompConnectionRejected as error:
            return WebSocketResult(self._error(error), 1008)
        except StompFrameRejected as error:
            return WebSocketResult(self._error(error))

    def disconnect(self, session_id: str) -> None:
        self.processor.disconnect(session_id)

    @staticmethod
    def _error(error: Exception) -> bytes:
        return StompFrame("ERROR", {"message": str(error)}).encode()
