from __future__ import annotations

import unittest
from datetime import UTC, datetime

from src.search import InMemorySearchHistory
from src.shared import (
    OpsSearchSnapshot,
    SearchAnswerHistoryRecord,
    SearchHistoryBundle,
    SearchHistoryRecord,
)


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)


class SearchOpsSnapshotTests(unittest.TestCase):
    def test_AC_OPS_002_search_projection_omits_query_answer_and_internal_fields(self):
        history = InMemorySearchHistory()
        history.record(SearchHistoryBundle(
            SearchHistoryRecord(
                "user", "private raw query", NOW, 9.0, 0, "FAILED", "internal-hash"
            ),
            SearchAnswerHistoryRecord(
                "user", NOW, "private raw answer", "FAILED", "internal-hash"
            ),
        ))

        snapshot = history.ops_search_snapshots(NOW)
        history.searches.clear()

        self.assertEqual((OpsSearchSnapshot(NOW),), snapshot)
        self.assertIsInstance(snapshot, tuple)
        rendered = repr(snapshot)
        self.assertNotIn("private raw query", rendered)
        self.assertNotIn("private raw answer", rendered)
        self.assertNotIn("internal-hash", rendered)

    def test_AC_OPS_001_search_projection_requires_aware_now(self):
        history = InMemorySearchHistory()

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            history.ops_search_snapshots(NOW.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
