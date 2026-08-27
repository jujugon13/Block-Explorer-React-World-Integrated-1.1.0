"""HTTP values shared by feature packages, without framework dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, TypeAlias
from urllib.parse import parse_qsl


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    user_id: int | None = None
    department_id: int | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    principal: Principal | None = None
    path_params: Mapping[str, str] = field(default_factory=dict)
    query_params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        path, separator, query = self.path.partition("?")
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "path", path or "/")
        object.__setattr__(
            self,
            "headers",
            {name.lower(): value for name, value in self.headers.items()},
        )
        object.__setattr__(self, "path_params", dict(self.path_params))
        parsed = dict(parse_qsl(query, keep_blank_values=True)) if separator else {}
        parsed.update(self.query_params)
        object.__setattr__(self, "query_params", parsed)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()


Handler: TypeAlias = Callable[[Request], object]
PrincipalResolver: TypeAlias = Callable[[Request], Principal | None]
WebSocketSink: TypeAlias = Callable[[bytes], None]


@dataclass(frozen=True, slots=True)
class WebSocketResult:
    payload: bytes | None = None
    close_code: int | None = None


class WebSocketGateway(Protocol):
    def open(self, origin: str | None, sink: WebSocketSink) -> str | None: ...

    def receive(self, session_id: str, payload: bytes) -> WebSocketResult: ...

    def disconnect(self, session_id: str) -> None: ...
