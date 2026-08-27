from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

from src.ops import CompositeOpsSnapshotReader, DashboardService
from src.shared import (
    OpsDocumentSnapshot,
    OpsJobSnapshot,
    OpsSearchSnapshot,
    OpsWorkerSnapshot,
)


NOW = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)


class _Documents:
    def __init__(self) -> None:
        self.rows = [OpsDocumentSnapshot("INDEXED")]
        self.calls: list[datetime] = []

    def ops_document_snapshots(self, now: datetime):
        self.calls.append(now)
        return self.rows


class _Indexing:
    def __init__(self) -> None:
        self.jobs = [OpsJobSnapshot("PENDING")]
        self.workers = [OpsWorkerSnapshot("ACTIVE", NOW)]
        self.calls: list[tuple[str, datetime]] = []

    def ops_job_snapshots(self, now: datetime):
        self.calls.append(("jobs", now))
        return self.jobs

    def ops_worker_snapshots(self, now: datetime):
        self.calls.append(("workers", now))
        return self.workers


class _Searches:
    def __init__(self) -> None:
        self.rows = [OpsSearchSnapshot(NOW - timedelta(hours=1))]
        self.calls: list[datetime] = []

    def ops_search_snapshots(self, now: datetime):
        self.calls.append(now)
        return self.rows


class CompositeOpsSnapshotTests(unittest.TestCase):
    def test_AC_OPS_002_combines_one_instant_into_immutable_tuple_snapshot(self):
        documents, indexing, searches = _Documents(), _Indexing(), _Searches()
        reader = CompositeOpsSnapshotReader(documents, indexing, searches)

        snapshot = reader.read_ops_snapshot(NOW)
        documents.rows.clear()
        indexing.jobs.clear()
        indexing.workers.clear()
        searches.rows.clear()

        self.assertEqual((NOW,), tuple(documents.calls))
        self.assertEqual((("jobs", NOW), ("workers", NOW)), tuple(indexing.calls))
        self.assertEqual((NOW,), tuple(searches.calls))
        self.assertEqual((1, 1, 1, 1), tuple(map(len, (
            snapshot.documents, snapshot.jobs, snapshot.workers, snapshot.searches
        ))))
        self.assertTrue(all(isinstance(rows, tuple) for rows in (
            snapshot.documents, snapshot.jobs, snapshot.workers, snapshot.searches
        )))
        with self.assertRaises(FrozenInstanceError):
            snapshot.documents = ()
        self.assertIsNone(
            DashboardService(reader, clock=lambda: NOW).summary()["jobs"]["avgProcessMs"]
        )

    def test_AC_OPS_001_rejects_naive_now_before_calling_sources(self):
        documents, indexing, searches = _Documents(), _Indexing(), _Searches()
        reader = CompositeOpsSnapshotReader(documents, indexing, searches)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            reader.read_ops_snapshot(NOW.replace(tzinfo=None))

        self.assertEqual([], documents.calls)
        self.assertEqual([], indexing.calls)
        self.assertEqual([], searches.calls)

    def test_AC_OPS_002_rejects_naive_source_instants_with_fixed_error(self):
        documents, indexing, searches = _Documents(), _Indexing(), _Searches()
        indexing.workers[:] = [OpsWorkerSnapshot("ACTIVE", NOW.replace(tzinfo=None))]

        with self.assertRaisesRegex(ValueError, "^operations instants must be timezone-aware$"):
            CompositeOpsSnapshotReader(documents, indexing, searches).read_ops_snapshot(NOW)

    def test_AC_OPS_001_hides_source_exception_text(self):
        class BrokenDocuments(_Documents):
            def ops_document_snapshots(self, now: datetime):
                raise RuntimeError("postgres://private-password@database")

        reader = CompositeOpsSnapshotReader(BrokenDocuments(), _Indexing(), _Searches())
        with self.assertRaises(RuntimeError) as caught:
            reader.read_ops_snapshot(NOW)

        self.assertEqual("operations snapshot unavailable", str(caught.exception))
        self.assertNotIn("private-password", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
