from __future__ import annotations

import unittest

from src.documents import DocumentWorkspace, UploadFile
from src.documents.testing import MemoryStorage
from src.mcp import McpApplicationBackend, McpToolService
from src.permissions import PermissionService
from src.shared import Principal, PublicError


OWNER = Principal("owner", frozenset({"USER"}), user_id=1)
OTHER = Principal("other", frozenset({"USER"}), user_id=2)


class _Search:
    def execute(self, payload, principal, *, debug=False):
        raise AssertionError("search is not used by document tools")


class McpIntegrationAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = DocumentWorkspace(MemoryStorage())
        created = self.documents.upload(
            OWNER,
            UploadFile(b"document", "document.txt", "text/plain"),
            title="Document",
            description=None,
            visibility="PRIVATE",
        )
        self.document_id = int(created.data["documentId"])
        permissions = PermissionService(self.documents)
        self.tools = McpToolService(
            McpApplicationBackend(_Search(), self.documents, permissions)
        )

    def test_AC_MCP_010_actual_permission_service_hides_unreadable_document(self):
        with self.assertRaises(PublicError) as caught:
            self.tools.call(
                "get_document_detail", {"documentId": self.document_id}, OTHER
            )
        self.assertEqual("ROLE-002", caught.exception.code)

    def test_AC_MCP_011_actual_permission_service_reports_readable_deleted_document(self):
        self.documents.delete(OWNER, self.document_id)
        with self.assertRaises(PublicError) as caught:
            self.tools.call(
                "get_document_detail", {"documentId": self.document_id}, OWNER
            )
        self.assertEqual("DOCUMENT-001", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
