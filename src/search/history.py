"""D-8 history retention boundary for a scheduler-owned periodic task."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, RLock

from src.shared import (
    OpsSearchSnapshot,
    SearchAnswerHistoryRecord,
    SearchCitationHistoryRecord,
    SearchHistoryBundle,
    SearchHistoryRecord,
    SearchHistoryWriter,
)


class InMemorySearchHistory:
    """Thread-safe reference adapter for the relational history port."""

    def __init__(self) -> None:
        self.searches: list[SearchHistoryRecord] = []
        self.answers: list[SearchAnswerHistoryRecord] = []
        self.citations: list[SearchCitationHistoryRecord] = []
        self._lock = RLock()

    def record(self, bundle: SearchHistoryBundle) -> None:
        with self._lock:
            self.searches.append(bundle.search)
            if bundle.answer is not None:
                self.answers.append(bundle.answer)
            self.citations.extend(bundle.citations)

    def purge_before(self, cutoff: datetime) -> None:
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        with self._lock:
            self.searches[:] = [
                row for row in self.searches if row.requested_at >= cutoff
            ]
            self.answers[:] = [
                row for row in self.answers if row.requested_at >= cutoff
            ]
            self.citations[:] = [
                row for row in self.citations if row.requested_at >= cutoff
            ]

    def ops_search_snapshots(self, now: datetime) -> tuple[OpsSearchSnapshot, ...]:
        """Return the dashboard projection; query and answer text never cross it."""

        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("operations instants must be timezone-aware")
        with self._lock:
            if any(
                row.requested_at.tzinfo is None
                or row.requested_at.utcoffset() is None
                for row in self.searches
            ):
                raise ValueError("operations instants must be timezone-aware")
            return tuple(OpsSearchSnapshot(row.requested_at) for row in self.searches)


class SearchHistoryRetentionJob:
    """Purge search, answer, and citation history after exactly 90 days."""

    RETENTION_DAYS = 90

    def __init__(
        self,
        history: SearchHistoryWriter,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._history = history
        self._clock = clock

    def run(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now - timedelta(days=self.RETENTION_DAYS)
        self._history.purge_before(cutoff)
        return cutoff

    def serve(self, stop: Event, *, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("history retention interval must be positive")
        while not stop.wait(interval_seconds):
            self.run()
