from __future__ import annotations

import unittest

from src.permissions import PermissionService
from src.permissions.test_sync_atomicity import NOW, OWNER, USER, _Documents, _Outbox
from src.shared import SyncEventRecord


class PermissionStage11IntegrationTests(unittest.TestCase):
    def test_IT_PERMISSION_001_refresh_effect_and_completion_share_rollback(self):
        service = PermissionService(_Documents(), sync_outbox=_Outbox(), clock=lambda: NOW)
        permission = service.grant(
            OWNER, "DOCUMENT", 1, "READ", target_type="USER", user_id=2
        )
        service._invalidate_permission(permission.permission_id)
        event = SyncEventRecord(
            "event-1",
            "PERMISSION",
            permission.permission_id,
            None,
            "PERMISSION_CACHE_REFRESH_REQUESTED",
            {
                "source": "DIRECT_DOCUMENT_PERMISSION",
                "permissionId": permission.permission_id,
                "action": "GRANT",
            },
            NOW,
        )

        def fail_completion() -> None:
            raise RuntimeError("completion failed")

        with self.assertRaisesRegex(RuntimeError, "completion failed"):
            service.commit_sync_permission_refresh(event, fail_completion)
        self.assertNotIn((USER.user_id, 1), service._user_cache)

        completed: list[bool] = []
        service.commit_sync_permission_refresh(event, lambda: completed.append(True))
        self.assertEqual([True], completed)
        self.assertIn(
            permission.permission_id,
            service._user_cache[(USER.user_id, 1)],
        )


if __name__ == "__main__":
    unittest.main()
