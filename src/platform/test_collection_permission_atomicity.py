from __future__ import annotations

import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier, RLock, Thread
from time import sleep

from src.collections import CollectionWorkspace
from src.permissions import PermissionService
from src.shared import Principal, PublicError, ResourceAccess
from src.sync import SyncService


NOW = datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
OWNER = Principal("owner", user_id=1)
READER = Principal("reader", user_id=2)


class Documents:
    def document_access(self, document_id, *, include_deleted=False):
        return ResourceAccess(document_id, 1, "PRIVATE", "INDEXED")

    def document_ids(self):
        return frozenset({1})


class CommitFailingOutbox:
    """Real sync ledger with a failure injected at transaction commit."""

    def __init__(self, service: SyncService) -> None:
        self.service = service
        self.fail_commit = False

    @contextmanager
    def transaction(self):
        with self.service.transaction():
            yield self
            if self.fail_commit:
                raise RuntimeError("outbox commit failed")

    def publish_permission_cache_refresh(self, *args, **kwargs):
        return self.service.publish_permission_cache_refresh(*args, **kwargs)


class YieldingRLock:
    """Yield after acquisition so inverse lock order deadlocks deterministically."""

    def __init__(self) -> None:
        self.lock = RLock()

    def __enter__(self):
        self.lock.acquire()
        sleep(0.01)
        return self

    def __exit__(self, *_):
        self.lock.release()


class CollectionPermissionAtomicityTests(unittest.TestCase):
    def test_AC_SYNC_002_AC_COL_002_cleanup_commit_failure_rolls_back_all_ledgers(self):
        sync = SyncService(clock=lambda: NOW)
        outbox = CommitFailingOutbox(sync)
        documents = Documents()
        permissions = PermissionService(
            documents,
            sync_outbox=outbox,
            clock=lambda: NOW,
        )
        collections = CollectionWorkspace(
            access_decider=permissions.collection_decider,
            documents=documents,
            permission_reader=permissions,
            invalidate_document_cache=permissions.invalidate_document_cache,
            cleanup_collection_permissions=permissions.cleanup_collection_permissions,
        )
        permissions.collections = collections

        root = collections.create(OWNER, "root")
        child = collections.create(OWNER, "child", parent_id=root.id)
        collections.add_document(OWNER, child.id, 1)
        root_permission = permissions.grant(
            OWNER,
            "COLLECTION",
            root.id,
            "READ",
            target_type="USER",
            user_id=2,
        )
        child_permission = permissions.grant(
            OWNER,
            "COLLECTION",
            child.id,
            "READ",
            target_type="USER",
            user_id=2,
        )
        permissions.readable_document_ids(READER, [1])

        permissions_before = dict(permissions._permissions)
        cache_before = deepcopy(permissions._user_cache)
        event_ids_before = tuple(event.id for event in sync.events())
        outbox.fail_commit = True

        with self.assertRaises(RuntimeError):
            collections.delete(OWNER, root.id)

        self.assertEqual("ACTIVE", root.status)
        self.assertEqual("ACTIVE", child.status)
        self.assertEqual(frozenset({1}), collections.document_ids_in_collection(child.id))
        self.assertEqual(permissions_before, permissions._permissions)
        self.assertEqual(cache_before, permissions._user_cache)
        self.assertEqual(event_ids_before, tuple(event.id for event in sync.events()))

        outbox.fail_commit = False
        collections.delete(OWNER, root.id)
        self.assertEqual("DELETED", root.status)
        self.assertEqual("DELETED", child.status)
        self.assertEqual(frozenset(), collections.document_ids_in_collection(child.id))
        self.assertNotIn(root_permission.permission_id, permissions._permissions)
        self.assertNotIn(child_permission.permission_id, permissions._permissions)
        self.assertEqual({}, permissions._user_cache)
        permission_events = sync.events(event_type="PERMISSION_CACHE_REFRESH_REQUESTED")
        self.assertEqual(4, len(permission_events))
        self.assertEqual(
            {"GRANT": 2, "REVOKE": 2},
            {
                action: sum(event.payload["action"] == action for event in permission_events)
                for action in ("GRANT", "REVOKE")
            },
        )
        self.assertEqual(
            {root_permission.permission_id, child_permission.permission_id},
            {
                event.aggregate_id
                for event in permission_events
                if event.payload["action"] == "REVOKE"
            },
        )

    def test_AC_COL_002_AC_PERM_003_delete_and_permission_read_do_not_deadlock(self):
        documents = Documents()
        permissions = PermissionService(documents, clock=lambda: NOW)
        collections = CollectionWorkspace(
            access_decider=permissions.collection_decider,
            documents=documents,
            permission_reader=permissions,
            invalidate_document_cache=permissions.invalidate_document_cache,
            cleanup_collection_permissions=permissions.cleanup_collection_permissions,
        )
        permissions.collections = collections
        root = collections.create(OWNER, "root")
        permissions.grant(
            OWNER,
            "COLLECTION",
            root.id,
            "READ",
            target_type="USER",
            user_id=2,
        )
        permissions._lock = YieldingRLock()
        collections._lock = YieldingRLock()
        start = Barrier(3)
        failures: list[Exception] = []

        def delete():
            start.wait()
            try:
                collections.delete(OWNER, root.id)
            except Exception as error:
                failures.append(error)

        def inspect():
            start.wait()
            try:
                permissions.list_direct(OWNER, "COLLECTION", root.id)
            except PublicError:
                pass
            except Exception as error:
                failures.append(error)

        threads = [Thread(target=delete), Thread(target=inspect)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(1)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
