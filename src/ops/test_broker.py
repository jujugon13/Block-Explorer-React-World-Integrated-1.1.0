from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from src.ops import (
    DASHBOARD_DESTINATION,
    DashboardDestinationPolicy,
    DashboardPush,
    DashboardService,
)
from src.ops.broker import DashboardBrokerPublisher
from src.platform.websocket import (
    InMemoryStompBroker,
    StompFrame,
    StompFrameProcessor,
    StompFrameRejected,
)
from src.shared import OpsSnapshot, Principal


NOW = datetime(2026, 8, 27, tzinfo=UTC)
USER = Principal("user@example.com", frozenset({"USER"}), user_id=1)
ADMIN = Principal("admin@example.com", frozenset({"USER", "ADMIN"}), user_id=2)


class _Reader:
    def read_ops_snapshot(self, now):
        return OpsSnapshot()


def _resolver(request):
    return {
        "Bearer user": USER,
        "Bearer admin": ADMIN,
    }.get(request.header("authorization"))


class DashboardBrokerAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = iter(f"session-{number}" for number in range(1, 20))
        self.broker = InMemoryStompBroker()
        self.processor = StompFrameProcessor(
            _resolver,
            DashboardDestinationPolicy(),
            self.broker,
            session_id=lambda: next(self.ids),
        )

    def connect(self, token: str, sink=lambda frame: None) -> str:
        session_id = self.processor.handshake("http://localhost:3000", sink)
        response = self.processor.process(
            session_id,
            StompFrame("CONNECT", {"Authorization": f"Bearer {token}"}),
        )
        self.assertEqual("CONNECTED", response.command)
        return session_id

    def test_AC_OPS_003_SUBSCRIBE_rechecks_ADMIN_at_the_destination(self):
        user_session = self.connect("user")
        with self.assertRaises(StompFrameRejected):
            self.processor.process(
                user_session,
                StompFrame(
                    "SUBSCRIBE",
                    {"id": "user-sub", "destination": DASHBOARD_DESTINATION},
                ),
            )
        self.assertEqual(0, self.broker.subscriber_count(DASHBOARD_DESTINATION))

        admin_session = self.connect("admin")
        self.processor.process(
            admin_session,
            StompFrame(
                "SUBSCRIBE",
                {"id": "admin-sub", "destination": DASHBOARD_DESTINATION},
            ),
        )
        self.assertEqual(1, self.broker.subscriber_count(DASHBOARD_DESTINATION))

    def test_AC_OPS_004_SEND_to_dashboard_is_denied_even_for_ADMIN(self):
        admin_session = self.connect("admin")
        with self.assertRaises(StompFrameRejected):
            self.processor.process(
                admin_session,
                StompFrame("SEND", {"destination": DASHBOARD_DESTINATION}, b"{}"),
            )
        self.assertEqual(0, self.broker.subscriber_count(DASHBOARD_DESTINATION))

    def test_AC_OPS_005_ten_commits_deliver_one_snapshot_and_never_replay(self):
        received: list[StompFrame] = []
        admin_session = self.connect("admin", received.append)
        self.processor.process(
            admin_session,
            StompFrame(
                "SUBSCRIBE",
                {"id": "dashboard", "destination": DASHBOARD_DESTINATION},
            ),
        )
        service = DashboardService(_Reader(), clock=lambda: NOW)
        push = DashboardPush(service, DashboardBrokerPublisher(self.broker))

        for _ in range(10):
            push.state_transition_committed()
        self.assertTrue(push.tick())
        self.assertFalse(push.tick())
        self.assertEqual(1, len(received))
        self.assertEqual("MESSAGE", received[0].command)
        self.assertEqual(DASHBOARD_DESTINATION, received[0].headers["destination"])
        self.assertEqual(
            {
                "documents": {"total": 0, "searchable": 0, "pendingIndex": 0},
                "jobs": {
                    "pending": 0,
                    "processing": 0,
                    "failed": 0,
                    "avgProcessMs": None,
                },
                "workers": {"activeCount": 0, "totalCount": 0},
                "search": {"recent24hCount": 0},
            },
            json.loads(received[0].body),
        )

        self.processor.disconnect(admin_session)
        push.state_transition_committed()
        self.assertTrue(push.tick())
        self.assertEqual(1, len(received))
        self.assertEqual(0, self.broker.subscriber_count(DASHBOARD_DESTINATION))


if __name__ == "__main__":
    unittest.main()
