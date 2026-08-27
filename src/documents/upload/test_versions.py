from __future__ import annotations

import threading
import unittest

from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.shared import Principal, PublicError


class DocumentVersionAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.owner = Principal("owner@example.com", user_id=1, display_name="Owner")
        self.other_admin = Principal(
            "admin@example.com", frozenset({"ADMIN"}), user_id=2, display_name="Admin"
        )
        self.workspace = DocumentWorkspace(self.storage)

    def _initial(self, data=b"one", filename="file.txt", content_type="text/plain"):
        return self.workspace.upload(
            self.owner,
            UploadFile(data, filename, content_type),
            title="Original title",
            description=None,
            visibility="PRIVATE",
        )

    def _new(self, document_id: int, data: bytes, filename="file.txt", content_type="text/plain"):
        return self.workspace.add_version(
            self.owner, document_id, UploadFile(data, filename, content_type)
        )

    def _assert_code(self, code: str, call) -> None:
        with self.assertRaises(PublicError) as raised:
            call()
        self.assertEqual(code, raised.exception.code)

    def test_AC_DOC_020_delegated_admin_is_not_owner(self):
        allowed = DocumentWorkspace(self.storage, access_decider=lambda principal, doc, action: True)
        first = allowed.upload(
            self.owner,
            UploadFile(b"one", "file.txt", "text/plain"),
            title="Title",
            description=None,
            visibility="PRIVATE",
        )
        allowed.set_version_state(first.data["documentId"], "INDEXED")
        self._assert_code(
            "ROLE-002",
            lambda: allowed.add_version(
                self.other_admin,
                first.data["documentId"],
                UploadFile(b"two", "file.txt", "text/plain"),
            ),
        )

    def test_AC_DOC_021_deleted_document_is_hidden(self):
        first = self._initial()
        document_id = first.data["documentId"]
        self.workspace.delete(self.owner, document_id)
        self._assert_code(
            "DOCUMENT-001", lambda: self._new(document_id, b"two")
        )

    def test_AC_DOC_022_processing_version_blocks_new_version(self):
        first = self._initial()
        self._assert_code(
            "DOCUMENT-VERSION-002",
            lambda: self._new(first.data["documentId"], b"two"),
        )

    def test_AC_DOC_023_same_binary_as_current_is_rejected(self):
        first = self._initial()
        document_id = first.data["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        self._assert_code(
            "DOCUMENT-VERSION-001", lambda: self._new(document_id, b"one")
        )

    def test_AC_DOC_024_cross_format_version_is_rejected(self):
        first = self._initial(b"pdf", "file.pdf", "application/pdf")
        document_id = first.data["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        self._assert_code(
            "DOCUMENT-VERSION-004",
            lambda: self._new(
                document_id,
                b"docx",
                "file.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        )

    def test_AC_DOC_025_failed_document_returns_to_uploaded(self):
        first = self._initial()
        document_id = first.data["documentId"]
        self.workspace.set_version_state(document_id, "FAILED")
        result = self._new(document_id, b"two")
        self.assertEqual(201, result.status)
        self.assertEqual("UPLOADED", result.data["documentStatus"])
        self.assertEqual("UPLOADED", result.data["versionStatus"])
        self.assertEqual("PENDING", result.data["jobStatus"])

    def test_AC_DOC_026_concurrent_version_upload_has_one_winner(self):
        first = self._initial()
        document_id = first.data["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        barrier = threading.Barrier(3)
        outcomes: list[tuple[str, object]] = []

        def run(data: bytes) -> None:
            barrier.wait()
            try:
                outcomes.append(("ok", self._new(document_id, data)))
            except PublicError as error:
                outcomes.append(("error", error.code))

        threads = [threading.Thread(target=run, args=(value,)) for value in (b"two", b"three")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(1, sum(kind == "ok" for kind, _ in outcomes))
        self.assertEqual([("error", "DOCUMENT-VERSION-002")], [item for item in outcomes if item[0] == "error"])
        self.assertEqual(2, len(self.workspace.versions(document_id)))
        self.assertEqual(2, len(self.storage.objects))

    def test_AC_DOC_027_version_number_is_max_plus_one(self):
        first = self._initial()
        document_id = first.data["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        for number in (2, 3):
            result = self._new(document_id, f"v{number}".encode())
            self.assertEqual(number, result.data["versionNo"])
            self.workspace.set_version_state(document_id, "INDEXED")
        fourth = self._new(document_id, b"v4")
        self.assertEqual(4, fourth.data["versionNo"])

    def test_AC_DOC_028_title_snapshots_do_not_change(self):
        first = self._initial()
        document_id = first.data["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        self.workspace.update_metadata(
            self.owner, document_id, title="Changed title", description="  description  "
        )
        second = self._new(document_id, b"two")
        versions = self.workspace.versions(document_id)
        self.assertEqual("Original title", versions[0].title_snapshot)
        self.assertEqual("Changed title", versions[1].title_snapshot)
        self.assertEqual(2, second.data["versionNo"])


if __name__ == "__main__":
    unittest.main()
