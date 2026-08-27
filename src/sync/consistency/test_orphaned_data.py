from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from src.shared import StorageLocation, StoredObject
from src.sync import SyncService

from .orphaned_data import OrphanedDataDetector


NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
OLD = NOW - timedelta(hours=25)
FRESH = NOW - timedelta(hours=23)
ROOT = "12345678-1234-5678-1234-567812345678"
ORPHAN = f"documents/{ROOT}/11111111-1111-1111-1111-111111111111.txt"
COMMITTED = f"documents/{ROOT}/22222222-2222-2222-2222-222222222222.txt"
FRESH_KEY = f"documents/{ROOT}/33333333-3333-3333-3333-333333333333.txt"


class _Inventory:
    provider = "s3"
    namespace = "bucket"

    def __init__(self, objects: list[StoredObject]) -> None:
        self.objects = list(objects)
        self.deleted: list[StorageLocation] = []

    def iter_objects(self, prefix: str):
        return (row for row in self.objects if row.location.key.startswith(prefix))

    def delete(self, location: StorageLocation) -> None:
        self.deleted.append(location)
        self.objects = [row for row in self.objects if row.location != location]


class _References:
    def __init__(self, referenced: set[str]) -> None:
        self.referenced = referenced
        self.calls: list[str] = []

    def is_referenced(self, location: StorageLocation) -> bool:
        self.calls.append(location.key)
        return location.key in self.referenced


def _object(key: str, modified: datetime) -> StoredObject:
    return StoredObject(StorageLocation("s3", "bucket", key, 1), modified)


class OrphanedDataDetectorTests(unittest.TestCase):
    def test_IT_ORPHAN_002_no_inventory_completes_without_findings(self):
        service = SyncService(clock=lambda: NOW)

        for mode in ("DRY_RUN", "REPAIR"):
            run = service.reconcile(mode=mode, actor_id="stage16")
            self.assertEqual("COMPLETED", run.status)
        self.assertEqual((), service.issues())

    def test_AC_SYNC_010_dry_run_reports_and_repair_deletes_only_aged_orphan(self):
        inventory = _Inventory([
            _object(ORPHAN, OLD),
            _object(COMMITTED, OLD),
            _object(FRESH_KEY, FRESH),
            _object("documents/not-a-candidate.txt", OLD),
        ])
        references = _References({COMMITTED})
        detector = OrphanedDataDetector(inventory, references, clock=lambda: NOW)
        service = SyncService(detector=detector, clock=lambda: NOW)

        service.reconcile(mode="DRY_RUN", actor_id="stage16")
        issues = service.issues(issue_type="ORPHANED_DATA")
        self.assertEqual(["OPEN"], [row.status for row in issues])
        self.assertEqual([], inventory.deleted)

        service.reconcile(mode="REPAIR", actor_id="stage16")
        self.assertEqual([ORPHAN], [row.key for row in inventory.deleted])
        self.assertEqual("RESOLVED", service.issue(issues[0].id).status)
        self.assertEqual(
            {COMMITTED, FRESH_KEY, "documents/not-a-candidate.txt"},
            {row.location.key for row in inventory.objects},
        )
        self.assertEqual(3, references.calls.count(ORPHAN))

    def test_IT_ORPHAN_001_reference_created_during_recheck_prevents_delete(self):
        inventory = _Inventory([_object(ORPHAN, OLD)])

        class _CommitRace:
            calls = 0

            def is_referenced(self, location: StorageLocation) -> bool:
                del location
                self.calls += 1
                return self.calls == 2

        detector = OrphanedDataDetector(
            inventory,
            _CommitRace(),
            clock=lambda: NOW,
        )

        detector.detect(cursor=None, mode="REPAIR", limit=100)

        self.assertEqual([], inventory.deleted)
        self.assertEqual([ORPHAN], [row.location.key for row in inventory.objects])


if __name__ == "__main__":
    unittest.main()
