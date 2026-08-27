from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime

from src.permissions import PermissionService
from src.shared import Identifier, Principal, ResourceAccess


NOW = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
OWNER = Principal("owner@example.com", user_id=1)
USER = Principal("user@example.com", user_id=2)


class _Documents:
    def document_access(self, document_id, *, include_deleted=False):
        return ResourceAccess(document_id, 1, "PRIVATE", "INDEXED")

    def document_ids(self):
        return frozenset({1})


class _Outbox:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.next_id = 1
        self.fail = False

    @contextmanager
    def transaction(self):
        events_before = list(self.events)
        next_id_before = self.next_id
        try:
            yield self
        except Exception:
            self.events = events_before
            self.next_id = next_id_before
            raise

    def publish_permission_cache_refresh(
        self,
        source: str,
        permission_id: Identifier,
        action: str,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ):
        event = {
            "id": self.next_id,
            "idempotencyKey": (
                f"PERMISSION:{source}:{permission_id}:"
                f"PERMISSION_CACHE_REFRESH_REQUESTED:{action}"
            ),
            "aggregateType": "PERMISSION",
            "aggregateId": permission_id,
            "eventType": "PERMISSION_CACHE_REFRESH_REQUESTED",
            "payload": payload,
            "occurredAt": occurred_at,
        }
        self.next_id += 1
        self.events.append(event)
        if self.fail:
            raise RuntimeError("outbox failed after insert")
        return event


class PermissionSyncAtomicityTests(unittest.TestCase):
    def service(self, outbox: _Outbox) -> PermissionService:
        return PermissionService(
            _Documents(), sync_outbox=outbox, clock=lambda: NOW
        )

    def test_AC_PERM_004_grant_rolls_back_ledger_cache_id_and_outbox(self):
        outbox = _Outbox()
        outbox.fail = True
        service = self.service(outbox)

        with self.assertRaises(RuntimeError):
            service.grant(
                OWNER,
                "DOCUMENT",
                1,
                "READ",
                target_type="USER",
                user_id=2,
            )

        self.assertEqual({}, service._permissions)
        self.assertEqual({}, service._user_cache)
        self.assertEqual(1, service._next_id)
        self.assertEqual([], outbox.events)
        self.assertEqual(1, outbox.next_id)

        outbox.fail = False
        permission = service.grant(
            OWNER,
            "DOCUMENT",
            1,
            "READ",
            target_type="USER",
            user_id=2,
        )
        self.assertEqual(1, permission.permission_id)
        self.assertEqual(frozenset({1}), service.readable_document_ids(USER, [1]))
        self.assertEqual(
            "PERMISSION:DIRECT_DOCUMENT_PERMISSION:1:"
            "PERMISSION_CACHE_REFRESH_REQUESTED:GRANT",
            outbox.events[0]["idempotencyKey"],
        )
        self.assertEqual(
            {
                "source": "DIRECT_DOCUMENT_PERMISSION",
                "permissionId": 1,
                "action": "GRANT",
            },
            outbox.events[0]["payload"],
        )

    def test_AC_PERM_004_revoke_rolls_back_ledger_cache_id_and_outbox(self):
        outbox = _Outbox()
        service = self.service(outbox)
        permission = service.grant(
            OWNER,
            "DOCUMENT",
            1,
            "READ",
            target_type="USER",
            user_id=2,
        )
        events_before = list(outbox.events)
        outbox.fail = True

        with self.assertRaises(RuntimeError):
            service.revoke(OWNER, "DOCUMENT", 1, permission.permission_id)

        self.assertIn(permission.permission_id, service._permissions)
        self.assertIn(permission.permission_id, service._user_cache[(2, 1)])
        self.assertEqual(2, service._next_id)
        self.assertEqual(events_before, outbox.events)
        self.assertTrue(service.can_read_document(USER, 1))

        outbox.fail = False
        service.revoke(OWNER, "DOCUMENT", 1, permission.permission_id)
        self.assertNotIn(permission.permission_id, service._permissions)
        self.assertNotIn((2, 1), service._user_cache)
        self.assertEqual(
            "PERMISSION:DIRECT_DOCUMENT_PERMISSION:1:"
            "PERMISSION_CACHE_REFRESH_REQUESTED:REVOKE",
            outbox.events[-1]["idempotencyKey"],
        )


if __name__ == "__main__":
    unittest.main()
