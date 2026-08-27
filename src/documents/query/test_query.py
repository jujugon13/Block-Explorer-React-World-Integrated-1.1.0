from __future__ import annotations

import hashlib
import unittest
from threading import Event, Thread

from src.documents import DocumentWorkspace, StoredChunk, UploadFile
from src.documents.testing import MemoryStorage
from src.permissions import PermissionService
from src.shared import Principal, PublicError


def _chunk(index: int, start: int, text: str) -> StoredChunk:
    return StoredChunk(
        index,
        start,
        start + len(text),
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        len(text.split()),
    )


class DocumentQueryAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.owner = Principal("owner@example.com", user_id=1, display_name="Owner")
        self.workspace = DocumentWorkspace(self.storage)

    def _initial(self, data=b"abcdefgh", title="Title"):
        return self.workspace.upload(
            self.owner,
            UploadFile(data, "file.txt", "text/plain"),
            title=title,
            description=None,
            visibility="PRIVATE",
        )

    def _assert_code(self, code: str, call) -> None:
        with self.assertRaises(PublicError) as raised:
            call()
        self.assertEqual(code, raised.exception.code)

    def test_AC_DOC_030_detail_requires_read_permission(self):
        denied = DocumentWorkspace(self.storage, access_decider=lambda principal, doc, action: False)
        first = denied.upload(
            self.owner,
            UploadFile(b"x", "file.txt", "text/plain"),
            title="Secret",
            description=None,
            visibility="PRIVATE",
        )
        self._assert_code(
            "ROLE-002", lambda: denied.detail(self.owner, first.data["documentId"])
        )

    def test_AC_DOC_030_concurrent_permission_grant_does_not_deadlock_detail(self):
        first = self._initial()
        document_id = first.data["documentId"]
        permission_locked, detail_waiting = Event(), Event()

        class PausingDocuments:
            pause_once = True

            def document_access(inner, identifier, *, include_deleted=False):
                if inner.pause_once:
                    inner.pause_once = False
                    permission_locked.set()
                    if not detail_waiting.wait(1):
                        raise TimeoutError("detail did not reach the permission decider")
                return self.workspace.document_access(
                    identifier, include_deleted=include_deleted
                )

        permissions = PermissionService(PausingDocuments())

        class SignalingDecider:
            @staticmethod
            def document_decider(principal, document, required):
                detail_waiting.set()
                return permissions.document_decider(principal, document, required)

        self.workspace.bind_permissions(SignalingDecider())
        reader = Principal("reader@example.com", user_id=2)
        errors: list[BaseException] = []
        details: list[dict[str, object]] = []

        def grant():
            try:
                permissions.grant(
                    self.owner,
                    "DOCUMENT",
                    document_id,
                    "READ",
                    target_type="USER",
                    user_id=reader.user_id,
                )
            except BaseException as error:
                errors.append(error)

        def read_detail():
            try:
                details.append(self.workspace.detail(reader, document_id))
            except BaseException as error:
                errors.append(error)

        grant_thread = Thread(target=grant, daemon=True)
        detail_thread = Thread(target=read_detail, daemon=True)
        grant_thread.start()
        self.assertTrue(permission_locked.wait(1))
        detail_thread.start()
        grant_thread.join(1)
        detail_thread.join(1)

        self.assertFalse(grant_thread.is_alive())
        self.assertFalse(detail_thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(document_id, details[0]["documentId"])

    def test_AC_DOC_031_first_processing_version_has_null_current(self):
        first = self._initial()
        status = self.workspace.status(self.owner, first.data["documentId"])
        self.assertIsNone(status["currentVersion"])
        self.assertEqual(
            {"versionNo": 1, "status": "UPLOADED", "jobStatus": "PENDING"},
            status["processingVersion"],
        )

    def test_AC_DOC_032_content_without_chunks_is_unavailable(self):
        first = self._initial()
        self._assert_code(
            "DOCUMENT-CONTENT-001",
            lambda: self.workspace.content(self.owner, first.data["documentId"]),
        )

    def test_AC_DOC_033_nonconsecutive_chunk_indices_are_corrupt(self):
        first = self._initial()
        version_id = first.data["documentVersionId"]
        self.workspace.put_chunks(version_id, (_chunk(0, 0, "abc"), _chunk(2, 2, "cde")))
        self._assert_code(
            "DOCUMENT-CHUNK-001",
            lambda: self.workspace.content(self.owner, first.data["documentId"]),
        )

    def test_AC_DOC_034_overlapping_chunks_rebuild_original(self):
        first = self._initial()
        version_id = first.data["documentVersionId"]
        self.workspace.put_chunks(version_id, (_chunk(0, 0, "abcde"), _chunk(1, 3, "defgh")))
        content = self.workspace.content(self.owner, first.data["documentId"])
        self.assertEqual("abcdefgh", content["content"])
        self.assertEqual(2, content["chunkCount"])

    def test_AC_DOC_035_invalid_disposition_is_rejected_before_storage_read(self):
        first = self._initial()
        self._assert_code(
            "COMMON-002",
            lambda: self.workspace.file(
                self.owner, first.data["documentId"], disposition="download"
            ),
        )
        self.assertEqual(0, self.storage.get_count)

    def test_AC_DOC_036_default_disposition_is_inline(self):
        first = self._initial()
        response = self.workspace.file(self.owner, first.data["documentId"])
        headers = dict(response.headers)
        self.assertEqual(200, response.status)
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual(str(len(response.body)), headers["Content-Length"])

    def test_AC_DOC_039_page_and_size_boundaries_are_validated(self):
        for page, size in ((-1, 20), (0, 101)):
            with self.subTest(page=page, size=size):
                self._assert_code(
                    "COMMON-002",
                    lambda page=page, size=size: self.workspace.list(
                        self.owner, page=page, size=size
                    ),
                )

    def test_AC_DOC_040_default_list_excludes_only_deleted(self):
        uploaded = self._initial(title="Uploaded")
        failed = self._initial(b"failed", title="Failed")
        indexed = self._initial(b"indexed", title="Indexed")
        deleted = self._initial(b"deleted", title="Deleted")
        self.workspace.set_version_state(failed.data["documentId"], "FAILED")
        self.workspace.set_version_state(indexed.data["documentId"], "INDEXED")
        deleted_version_count = len(self.workspace.versions(deleted.data["documentId"]))
        deleted_file_count = len(self.workspace.state.files)
        self.workspace.delete(self.owner, deleted.data["documentId"])
        page = self.workspace.list(self.owner)
        self.assertEqual(3, page["totalElements"])
        self.assertEqual(
            {"UPLOADED", "FAILED", "INDEXED"},
            {item["status"] for item in page["content"]},
        )
        self.assertEqual(deleted_version_count, len(self.workspace.versions(deleted.data["documentId"])))
        self.assertEqual(deleted_file_count, len(self.workspace.state.files))
        self.assertEqual("UPLOADED", self.workspace.state.documents[uploaded.data["documentId"]].status)


if __name__ == "__main__":
    unittest.main()
