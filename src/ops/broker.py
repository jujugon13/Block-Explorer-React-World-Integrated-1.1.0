"""Dashboard publisher backed by the volatile STOMP broker."""

from __future__ import annotations

import json
from collections.abc import Mapping

from src.shared import StompMessageBroker


class DashboardBrokerPublisher:
    """Serialize one complete dashboard snapshot for current subscribers."""

    def __init__(self, broker: StompMessageBroker) -> None:
        self._broker = broker

    @property
    def broker(self) -> StompMessageBroker:
        """Expose the bound broker so the composition root can reject split buses."""

        return self._broker

    def publish(self, destination: str, payload: Mapping[str, object]) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._broker.publish(destination, body, "application/json;charset=utf-8")
