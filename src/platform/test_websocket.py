from __future__ import annotations

import unittest

from src.platform.websocket import (
    InMemoryStompBroker,
    StompConnectionRejected,
    StompFrame,
    StompFrameProcessor,
)
from src.shared import Principal


USER = Principal("user@example.com", frozenset({"USER"}), user_id=1)


class _Policy:
    @staticmethod
    def can_subscribe(destination, principal):
        return principal is not None

    @staticmethod
    def can_send(destination, principal):
        return False


class WebSocketAcceptanceTests(unittest.TestCase):
    def test_AC_SYS_004_handshake_is_open_but_CONNECT_requires_native_bearer(self):
        identifiers = iter(("missing", "damaged", "valid"))

        def resolver(request):
            return USER if request.header("authorization") == "Bearer valid" else None

        processor = StompFrameProcessor(
            resolver,
            _Policy(),
            InMemoryStompBroker(),
            session_id=lambda: next(identifiers),
        )

        missing = processor.handshake("http://localhost:3000", lambda frame: None)
        with self.assertRaises(StompConnectionRejected):
            processor.process(missing, StompFrame("CONNECT"))

        damaged = processor.handshake("http://localhost:3000", lambda frame: None)
        with self.assertRaises(StompConnectionRejected):
            processor.process(
                damaged, StompFrame("CONNECT", {"Authorization": "Bearer damaged"})
            )

        valid = processor.handshake("http://localhost:3000", lambda frame: None)
        response = processor.process_raw(
            valid, b"CONNECT\nAuthorization:Bearer valid\n\n\x00"
        )
        self.assertEqual(b"CONNECTED\nversion:1.2\n\n\x00", response)


if __name__ == "__main__":
    unittest.main()
