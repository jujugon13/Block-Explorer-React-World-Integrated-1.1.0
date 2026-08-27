from __future__ import annotations

import asyncio
import unittest

from src.infra.http import create_fastapi_app
from src.shared import Response, WebSocketResult


class _Gateway:
    def __init__(self) -> None:
        self.sink = None
        self.received = []
        self.disconnected = []

    def open(self, origin, sink):
        self.sink = sink
        return "session"

    def receive(self, session_id, payload):
        self.received.append((session_id, payload))
        return WebSocketResult(b"CONNECTED\nversion:1.2\n\n\x00")

    def disconnect(self, session_id):
        self.disconnected.append(session_id)


async def _exchange(app, gateway):
    incoming = asyncio.Queue()
    outgoing = asyncio.Queue()

    async def receive():
        return await incoming.get()

    async def send(message):
        await outgoing.put(message)

    await incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(
        app(
            {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "scheme": "ws",
                "path": "/ws",
                "raw_path": b"/ws",
                "query_string": b"",
                "headers": [(b"origin", b"http://localhost:3000")],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 80),
                "root_path": "",
                "subprotocols": [],
            },
            receive,
            send,
        )
    )
    accepted = await outgoing.get()
    await incoming.put({"type": "websocket.receive", "text": "CONNECT\n\n\x00"})
    connected = await outgoing.get()
    gateway.sink(b"MESSAGE\n\n{}\x00")
    pushed = await outgoing.get()
    await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await task
    return accepted, connected, pushed


class FastApiWebSocketAdapterTests(unittest.TestCase):
    def test_IT_HTTP_003_native_websocket_routes_frames_and_push(self):
        gateway = _Gateway()
        app = create_fastapi_app(lambda request: Response(404), gateway)

        accepted, connected, pushed = asyncio.run(_exchange(app, gateway))

        self.assertEqual("websocket.accept", accepted["type"])
        self.assertEqual(
            {"type": "websocket.send", "text": "CONNECTED\nversion:1.2\n\n\x00"},
            connected,
        )
        self.assertEqual(
            {"type": "websocket.send", "text": "MESSAGE\n\n{}\x00"}, pushed
        )
        self.assertEqual([("session", b"CONNECT\n\n\x00")], gateway.received)
        self.assertEqual(["session"], gateway.disconnected)


if __name__ == "__main__":
    unittest.main()
