from __future__ import annotations

import unicodedata
import unittest

from src.documents import MAX_FILE_SIZE, DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.shared import Principal, PublicError


class DocumentUploadAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.workspace = DocumentWorkspace(self.storage)
        self.owner = Principal("owner@example.com", user_id=1, display_name="Owner")

    def _upload(
        self,
        data: bytes = b"hello",
        filename: str = "note.txt",
        content_type: str = "text/plain",
        **values,
    ):
        return self.workspace.upload(
            self.owner,
            UploadFile(data, filename, content_type),
            title=values.get("title", "Title"),
            description=values.get("description"),
            visibility=values.get("visibility", "PRIVATE"),
        )

    def _assert_code(self, code: str, call) -> None:
        with self.assertRaises(PublicError) as raised:
            call()
        self.assertEqual(code, raised.exception.code)

    def test_AC_DOC_001_zero_byte_file_is_rejected(self):
        self._assert_code("DOCUMENT-FILE-001", lambda: self._upload(b""))
        self.assertEqual({}, self.storage.objects)
        self.assertEqual({}, self.workspace.state.documents)

    def test_AC_DOC_002_exactly_50_mib_is_allowed(self):
        result = self._upload(b"x" * MAX_FILE_SIZE)
        self.assertEqual(201, result.status)
        self.assertEqual(MAX_FILE_SIZE, next(iter(self.workspace.state.files.values())).size)

    def test_AC_DOC_003_more_than_50_mib_is_rejected(self):
        self._assert_code(
            "DOCUMENT-FILE-002", lambda: self._upload(b"x" * (MAX_FILE_SIZE + 1))
        )
        self.assertEqual(0, self.storage.put_count)

    def test_AC_DOC_004_parent_path_filename_is_rejected(self):
        self._assert_code(
            "DOCUMENT-FILE-005", lambda: self._upload(filename="../evil.txt")
        )

    def test_AC_DOC_005_control_character_filename_is_rejected(self):
        self._assert_code(
            "DOCUMENT-FILE-005", lambda: self._upload(filename="bad\x1fname.txt")
        )

    def test_AC_DOC_006_extensionless_filename_is_rejected(self):
        self._assert_code("DOCUMENT-FILE-003", lambda: self._upload(filename="README"))

    def test_AC_DOC_007_markdown_may_use_text_plain(self):
        result = self._upload(filename="README.MD", content_type="TEXT/PLAIN")
        self.assertEqual(201, result.status)
        document = self.workspace.state.documents[result.data["documentId"]]
        self.assertEqual("MD", document.document_type)

    def test_AC_DOC_008_pdf_with_text_plain_is_rejected(self):
        self._assert_code(
            "DOCUMENT-FILE-004",
            lambda: self._upload(filename="file.pdf", content_type="text/plain"),
        )

    def test_AC_DOC_009_legacy_doc_is_rejected(self):
        self._assert_code(
            "DOCUMENT-FILE-003",
            lambda: self._upload(filename="file.doc", content_type="application/msword"),
        )

    def test_AC_DOC_010_nfd_filename_is_returned_as_nfc(self):
        nfd = unicodedata.normalize("NFD", "한글.txt")
        result = self._upload(filename=nfd)
        response = self.workspace.file(self.owner, result.data["documentId"])
        headers = dict(response.headers)
        self.assertIn("%ED%95%9C%EA%B8%80.txt", headers["Content-Disposition"])
        stored = next(iter(self.workspace.state.files.values()))
        self.assertTrue(unicodedata.is_normalized("NFC", stored.filename))
        self.assertEqual(unicodedata.normalize("NFC", nfd), stored.filename)

    def test_AC_DOC_014_initial_response_and_ledger_are_atomic(self):
        result = self._upload(
            title="  Kept title  ", description="   ", visibility="COLLECTION"
        )
        self.assertEqual(201, result.status)
        self.assertEqual(
            {
                "documentId",
                "documentVersionId",
                "fileObjectId",
                "embeddingJobId",
                "documentStatus",
                "jobStatus",
            },
            set(result.data),
        )
        self.assertEqual("UPLOADED", result.data["documentStatus"])
        self.assertEqual("PENDING", result.data["jobStatus"])
        document = self.workspace.state.documents[result.data["documentId"]]
        self.assertEqual("Kept title", document.title)
        self.assertIsNone(document.description)
        self.assertEqual("COLLECTION", document.visibility)
        self._assert_code(
            "DOCUMENT-VISIBILITY-001",
            lambda: self.workspace.update_visibility(
                self.owner, document.id, "DEPARTMENT"
            ),
        )

        failed_storage = MemoryStorage()
        failed_workspace = DocumentWorkspace(failed_storage)
        failed_workspace.fail_next_commit = True
        with self.assertRaises(RuntimeError):
            failed_workspace.upload(
                self.owner,
                UploadFile(b"candidate", "file.txt", "text/plain"),
                title="Title",
                description=None,
                visibility="PRIVATE",
            )
        self.assertEqual({}, failed_storage.objects)
        self.assertEqual({}, failed_workspace.state.documents)


if __name__ == "__main__":
    unittest.main()
