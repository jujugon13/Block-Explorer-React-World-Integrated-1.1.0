from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import UTC, datetime

from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.shared import Principal
from src.sync import SyncService


NOW = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)
OWNER = Principal("owner@example.com", user_id=1, display_name="Owner")


class _MutateThenFailOutbox(SyncService):
    def __init__(self) -> None:
        super().__init__(clock=lambda: NOW)
        self.fail_version = False
        self.fail_delete = False

    def publish_document_version_created(self, *args, **kwargs):
        event = super().publish_document_version_created(*args, **kwargs)
        if self.fail_version:
            raise RuntimeError("outbox failed after insert")
        return event

    def publish_document_deleted(self, *args, **kwargs):
        event = super().publish_document_deleted(*args, **kwargs)
        if self.fail_delete:
            raise RuntimeError("outbox failed after insert")
        return event


def _upload(workspace: DocumentWorkspace, data: bytes = b"one"):
    return workspace.upload(
        OWNER,
        UploadFile(data, "file.txt", "text/plain"),
        title="Title",
        description=None,
        visibility="PRIVATE",
    )


class DocumentSyncAtomicityTests(unittest.TestCase):
    def test_AC_SYNC_001_upload_and_mutated_outbox_roll_back_together(self):
        storage = MemoryStorage()
        outbox = _MutateThenFailOutbox()
        outbox.fail_version = True
        workspace = DocumentWorkspace(storage, sync_outbox=outbox, clock=lambda: NOW)

        with self.assertRaises(RuntimeError):
            _upload(workspace)

        self.assertEqual({}, workspace.state.documents)
        self.assertEqual({}, workspace.state.versions)
        self.assertEqual({}, workspace.state.jobs)
        self.assertEqual([], workspace.state.events)
        self.assertEqual(
            {"file": 1, "document": 1, "version": 1, "job": 1, "event": 1},
            workspace._next_ids,
        )
        self.assertEqual((), outbox.events())
        self.assertEqual({}, storage.objects)

    def test_AC_SYNC_001_add_version_and_mutated_outbox_roll_back_together(self):
        storage = MemoryStorage()
        outbox = _MutateThenFailOutbox()
        workspace = DocumentWorkspace(storage, sync_outbox=outbox, clock=lambda: NOW)
        first = _upload(workspace)
        document_id = first.data["documentId"]
        workspace.set_version_state(document_id, "INDEXED")
        state_before = deepcopy(workspace.state)
        ids_before = dict(workspace._next_ids)
        objects_before = dict(storage.objects)
        event_ids_before = tuple(event.id for event in outbox.events())
        outbox.fail_version = True

        with self.assertRaises(RuntimeError):
            workspace.add_version(
                OWNER,
                document_id,
                UploadFile(b"two", "file.txt", "text/plain"),
            )

        self.assertEqual(state_before, workspace.state)
        self.assertEqual(ids_before, workspace._next_ids)
        self.assertEqual(objects_before, storage.objects)
        self.assertEqual(event_ids_before, tuple(event.id for event in outbox.events()))

    def test_AC_SYNC_004_delete_and_mutated_outbox_roll_back_together(self):
        storage = MemoryStorage()
        outbox = _MutateThenFailOutbox()
        workspace = DocumentWorkspace(storage, sync_outbox=outbox, clock=lambda: NOW)
        first = _upload(workspace)
        document_id = first.data["documentId"]
        state_before = deepcopy(workspace.state)
        ids_before = dict(workspace._next_ids)
        event_ids_before = tuple(event.id for event in outbox.events())
        outbox.fail_delete = True

        with self.assertRaises(RuntimeError):
            workspace.delete(OWNER, document_id)

        self.assertEqual(state_before, workspace.state)
        self.assertEqual(ids_before, workspace._next_ids)
        self.assertEqual(event_ids_before, tuple(event.id for event in outbox.events()))
        self.assertNotEqual("DELETED", workspace.state.documents[document_id].status)


if __name__ == "__main__":
    unittest.main()
