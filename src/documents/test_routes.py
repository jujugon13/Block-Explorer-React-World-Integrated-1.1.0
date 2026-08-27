from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
import unittest
from datetime import UTC, datetime
from urllib.parse import quote

from src.documents import (
    DocumentWorkspace,
    StoredChunk,
    register_document_routes,
)
from src.documents.testing import MemoryStorage
from src.infra.http.testing import request_over_uvicorn
from src.platform import MAX_REQUEST_BYTES, PlatformApp
from src.shared import Principal, Request, StorageLocation


NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
OWNER = Principal("owner@example.com", user_id=1, display_name="Owner")
OTHER = Principal("other@example.com", user_id=2, display_name="Other")


def _resolver(request):
    return {
        "Bearer owner": OWNER,
        "Bearer other": OTHER,
    }.get(request.header("authorization"))


def _call(app, method: str, target: str, *, headers=None, body=b""):
    response = request_over_uvicorn(
        app.handle, Request(method, target, headers or {}, body)
    )
    return response.status, dict(response.headers), response.body


def _multipart(
    data: bytes | None,
    *,
    filename: str = "original.txt",
    content_type: str = "text/plain",
    fields: dict[str, str] | None = None,
):
    boundary = "vectorshelf-boundary"
    body = bytearray()
    for name, value in (fields or {}).items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    if data is not None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(data)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class DocumentRouteAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.workspace = DocumentWorkspace(self.storage, clock=lambda: NOW)
        self.app = PlatformApp(_resolver, lambda: NOW)
        register_document_routes(self.app, self.workspace)

    def _upload(self, data: bytes = b"document bytes") -> dict[str, object]:
        status, _, raw = self._upload_response(data)
        self.assertEqual(201, status)
        return json.loads(raw)["data"]

    def _upload_response(
        self,
        data: bytes,
        *,
        filename: str = "original.txt",
        file_content_type: str = "text/plain",
        title: str = "Title",
    ):
        body, content_type = _multipart(
            data,
            filename=filename,
            content_type=file_content_type,
            fields={
                "title": title,
                "description": "Description",
                "visibility": "PRIVATE",
            },
        )
        status, _, raw = _call(
            self.app,
            "POST",
            "/api/documents",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=body,
        )
        return status, _, raw

    def _version_response(
        self,
        document_id: int,
        data: bytes,
        *,
        authorization: str = "Bearer owner",
        filename: str = "original.txt",
        file_content_type: str = "text/plain",
    ):
        body, content_type = _multipart(
            data, filename=filename, content_type=file_content_type
        )
        return _call(
            self.app,
            "POST",
            f"/api/documents/{document_id}/versions",
            headers={"Authorization": authorization, "Content-Type": content_type},
            body=body,
        )

    def test_AC_DOC_002_AC_DOC_003_file_size_boundaries_over_uvicorn(self):
        status, _, _ = self._upload_response(b"x" * (50 * 1024 * 1024))
        self.assertEqual(201, status)

        status, _, raw = self._upload_response(b"y" * (50 * 1024 * 1024 + 1))
        self.assertEqual((400, "DOCUMENT-FILE-002"), (status, json.loads(raw)["code"]))

        fields = {"title": "Title", "description": "", "visibility": "PRIVATE"}
        base, content_type = _multipart(b"x", fields=fields)
        fields["description"] = "d" * (MAX_REQUEST_BYTES - len(base))
        exact, content_type = _multipart(b"x", fields=fields)
        self.assertEqual(MAX_REQUEST_BYTES, len(exact))
        status, _, _ = _call(
            self.app,
            "POST",
            "/api/documents",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=exact,
        )
        self.assertEqual(201, status)

        over = exact + b"x"
        status, _, raw = _call(
            self.app,
            "POST",
            "/api/documents",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=over,
        )
        self.assertEqual((400, "DOCUMENT-FILE-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_004_AC_DOC_005_AC_DOC_006_AC_DOC_007_AC_DOC_008_AC_DOC_009_file_validation_over_uvicorn(self):
        cases = (
            (b"x", "../evil.txt", "text/plain", 400, "DOCUMENT-FILE-005"),
            (b"x", "bad\x1fname.txt", "text/plain", 400, "DOCUMENT-FILE-005"),
            (b"x", "README", "text/plain", 400, "DOCUMENT-FILE-003"),
            (b"x", "README.md", "text/plain", 201, None),
            (b"x", "file.pdf", "text/plain", 400, "DOCUMENT-FILE-004"),
            (b"x", "file.doc", "application/msword", 400, "DOCUMENT-FILE-003"),
        )
        for data, filename, content_type, expected, code in cases:
            with self.subTest(filename=filename):
                status, _, raw = self._upload_response(
                    data, filename=filename, file_content_type=content_type
                )
                self.assertEqual(expected, status)
                if code is not None:
                    self.assertEqual(code, json.loads(raw)["code"])

    def test_AC_DOC_010_nfd_filename_is_nfc_in_uvicorn_response_header(self):
        filename = unicodedata.normalize("NFD", "한글.txt")
        result = json.loads(self._upload_response(b"x", filename=filename)[2])["data"]
        status, headers, _ = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}/file",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual(200, status)
        self.assertIn(quote(unicodedata.normalize("NFC", filename)), headers["Content-Disposition"])

    def test_AC_DOC_013_storage_location_mismatch_maps_over_uvicorn(self):
        result = self._upload(b"same")
        stored = self.workspace.state.files[result["fileObjectId"]]
        stored.location = StorageLocation(
            "other", stored.location.namespace, stored.location.key, stored.location.size
        )
        status, _, raw = self._upload_response(b"same", title="Other")
        self.assertEqual((500, "DOCUMENT-STORAGE-003"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_020_AC_DOC_021_AC_DOC_022_version_failures_over_uvicorn(self):
        first = self._upload(b"one")
        document_id = first["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        status, _, raw = self._version_response(
            document_id, b"two", authorization="Bearer other"
        )
        self.assertEqual((403, "ROLE-002"), (status, json.loads(raw)["code"]))

        self.workspace.delete(OWNER, document_id)
        status, _, raw = self._version_response(document_id, b"two")
        self.assertEqual((404, "DOCUMENT-001"), (status, json.loads(raw)["code"]))

        second = self._upload(b"processing")
        status, _, raw = self._version_response(second["documentId"], b"two")
        self.assertEqual(
            (409, "DOCUMENT-VERSION-002"), (status, json.loads(raw)["code"])
        )

    def test_AC_DOC_023_AC_DOC_024_AC_DOC_025_version_results_over_uvicorn(self):
        first = self._upload(b"one")
        document_id = first["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        status, _, raw = self._version_response(document_id, b"one")
        self.assertEqual(
            (409, "DOCUMENT-VERSION-001"), (status, json.loads(raw)["code"])
        )

        pdf = json.loads(
            self._upload_response(
                b"pdf", filename="file.pdf", file_content_type="application/pdf"
            )[2]
        )["data"]
        self.workspace.set_version_state(pdf["documentId"], "INDEXED")
        status, _, raw = self._version_response(
            pdf["documentId"],
            b"docx",
            filename="file.docx",
            file_content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(
            (400, "DOCUMENT-VERSION-004"), (status, json.loads(raw)["code"])
        )

        failed = self._upload(b"failed")
        self.workspace.set_version_state(failed["documentId"], "FAILED")
        status, _, raw = self._version_response(failed["documentId"], b"new")
        data = json.loads(raw)["data"]
        self.assertEqual((201, "UPLOADED"), (status, data["documentStatus"]))

    def test_AC_DOC_026_concurrent_version_requests_have_one_winner_over_uvicorn(self):
        first = self._upload(b"one")
        document_id = first["documentId"]
        self.workspace.set_version_state(document_id, "INDEXED")
        barrier = threading.Barrier(3)
        statuses = []

        def upload(data):
            barrier.wait()
            statuses.append(self._version_response(document_id, data)[0])

        threads = [threading.Thread(target=upload, args=(data,)) for data in (b"two", b"three")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(10)
        self.assertEqual([201, 409], sorted(statuses))

    def test_AC_DOC_032_AC_DOC_033_content_errors_over_uvicorn(self):
        first = self._upload(b"abcde")
        path = f"/api/documents/{first['documentId']}/content"
        status, _, raw = _call(
            self.app, "GET", path, headers={"Authorization": "Bearer owner"}
        )
        self.assertEqual(
            (409, "DOCUMENT-CONTENT-001"), (status, json.loads(raw)["code"])
        )

        self.workspace.put_chunks(
            first["documentVersionId"],
            (
                StoredChunk(0, 0, 3, "abc", hashlib.sha256(b"abc").hexdigest(), 1),
                StoredChunk(2, 2, 5, "cde", hashlib.sha256(b"cde").hexdigest(), 1),
            ),
        )
        status, _, raw = _call(
            self.app, "GET", path, headers={"Authorization": "Bearer owner"}
        )
        self.assertEqual((500, "DOCUMENT-CHUNK-001"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_037_AC_DOC_038_storage_read_errors_over_uvicorn(self):
        missing = self._upload(b"missing")
        stored = self.workspace.state.files[missing["fileObjectId"]]
        self.storage.objects.pop(stored.location.key)
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{missing['documentId']}/file",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual(
            (404, "DOCUMENT-STORAGE-002"), (status, json.loads(raw)["code"])
        )

        mismatched = self._upload(b"expected")
        stored = self.workspace.state.files[mismatched["fileObjectId"]]
        self.storage.objects[stored.location.key] = b"x"
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{mismatched['documentId']}/file",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual(
            (503, "DOCUMENT-STORAGE-001"), (status, json.loads(raw)["code"])
        )

    def test_AC_DOC_014_multipart_upload_preserves_file_and_detail_contract(self):
        binary = b"\x00first\r\n\xfflast"
        result = self._upload(binary)
        self.assertEqual(
            {
                "documentId",
                "documentVersionId",
                "fileObjectId",
                "embeddingJobId",
                "documentStatus",
                "jobStatus",
            },
            set(result),
        )
        stored = self.workspace.state.files[result["fileObjectId"]]
        self.assertEqual("original.txt", stored.filename)
        self.assertEqual("text/plain", stored.content_type)
        self.assertEqual(binary, self.storage.get(stored.location))

        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}",
            headers={"Authorization": "Bearer owner"},
        )
        detail = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "documentId",
                "title",
                "description",
                "documentType",
                "sourceType",
                "status",
                "visibility",
                "ownerUserId",
                "ownerName",
                "currentVersion",
                "contentAvailable",
                "createdAt",
                "updatedAt",
            },
            set(detail),
        )

    def test_AC_DOC_001_missing_file_uses_fixed_upload_error(self):
        body, content_type = _multipart(
            None,
            fields={"title": "Title", "visibility": "PRIVATE"},
        )
        status, _, raw = _call(
            self.app,
            "POST",
            "/api/documents",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=body,
        )
        error = json.loads(raw)
        self.assertEqual((400, "DOCUMENT-FILE-001"), (status, error["code"]))

    def test_AC_DOC_027_AC_DOC_031_version_and_status_routes(self):
        result = self._upload()
        document_id = result["documentId"]
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{document_id}/status",
            headers={"Authorization": "Bearer owner"},
        )
        state = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertIsNone(state["currentVersion"])
        self.assertEqual(
            {"versionNo": 1, "status": "UPLOADED", "jobStatus": "PENDING"},
            state["processingVersion"],
        )

        self.workspace.set_version_state(document_id, "INDEXED")
        body, content_type = _multipart(b"second version")
        status, _, raw = _call(
            self.app,
            "POST",
            f"/api/documents/{document_id}/versions",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=body,
        )
        version = json.loads(raw)["data"]
        self.assertEqual(201, status)
        self.assertEqual(2, version["versionNo"])
        self.assertEqual(
            {
                "documentId",
                "documentVersionId",
                "versionNo",
                "embeddingJobId",
                "currentVersionId",
                "documentStatus",
                "versionStatus",
                "jobStatus",
            },
            set(version),
        )

    def test_AC_DOC_030_detail_route_enforces_read_permission(self):
        result = self._upload()
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}",
            headers={"Authorization": "Bearer other"},
        )
        self.assertEqual((403, "ROLE-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_034_content_route_rebuilds_overlapping_chunks(self):
        result = self._upload(b"abcdefgh")
        chunks = (
            StoredChunk(0, 0, 5, "abcde", hashlib.sha256(b"abcde").hexdigest(), 1),
            StoredChunk(1, 3, 8, "defgh", hashlib.sha256(b"defgh").hexdigest(), 1),
        )
        self.workspace.put_chunks(result["documentVersionId"], chunks)
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}/content",
            headers={"Authorization": "Bearer owner"},
        )
        content = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertEqual("abcdefgh", content["content"])
        self.assertEqual(
            {"documentId", "documentVersionId", "versionNo", "content", "chunkCount"},
            set(content),
        )

    def test_AC_DOC_035_AC_DOC_036_file_route_handles_disposition_and_raw_binary(self):
        binary = b"raw\x00file\r\nbytes"
        result = self._upload(binary)
        path = f"/api/documents/{result['documentId']}/file"
        status, headers, raw = _call(
            self.app,
            "GET",
            path,
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((200, binary), (status, raw))
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        self.assertEqual("text/plain", headers["Content-Type"])

        status, headers, raw = _call(
            self.app,
            "GET",
            path + "?disposition=attachment",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((200, binary), (status, raw))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

        status, _, raw = _call(
            self.app,
            "GET",
            path + "?disposition=download",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((400, "COMMON-002"), (status, json.loads(raw)["code"]))

        status, _, raw = _call(
            self.app,
            "GET",
            path + "?disposition=",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((400, "COMMON-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_039_list_route_parses_and_validates_query_parameters(self):
        first = self._upload(b"first")
        second = self._upload(b"second")
        status, _, raw = _call(
            self.app,
            "GET",
            "/api/documents?status=UPLOADED&page=1&size=1",
            headers={"Authorization": "Bearer owner"},
        )
        page = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertEqual(2, page["totalElements"])
        self.assertEqual(second["documentId"], page["content"][0]["documentId"])
        self.assertNotEqual(first["documentId"], second["documentId"])

        status, _, raw = _call(
            self.app,
            "GET",
            "/api/documents?page=-1",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((400, "COMMON-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_040_metadata_visibility_delete_and_default_list_routes(self):
        result = self._upload()
        document_id = result["documentId"]
        auth_json = {
            "Authorization": "Bearer owner",
            "Content-Type": "application/json",
        }
        status, _, raw = _call(
            self.app,
            "PATCH",
            f"/api/documents/{document_id}",
            headers=auth_json,
            body=json.dumps({"title": "Changed", "description": None}).encode(),
        )
        self.assertEqual((204, b""), (status, raw))

        status, _, raw = _call(
            self.app,
            "PATCH",
            f"/api/documents/{document_id}/visibility",
            headers=auth_json,
            body=b'{"visibility":"PUBLIC"}',
        )
        self.assertEqual((204, b""), (status, raw))
        detail = json.loads(
            _call(
                self.app,
                "GET",
                f"/api/documents/{document_id}",
                headers={"Authorization": "Bearer owner"},
            )[2]
        )["data"]
        self.assertEqual(("Changed", None, "PUBLIC"), (
            detail["title"], detail["description"], detail["visibility"]
        ))

        status, _, raw = _call(
            self.app,
            "DELETE",
            f"/api/documents/{document_id}",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((204, b""), (status, raw))
        page = json.loads(
            _call(
                self.app,
                "GET",
                "/api/documents",
                headers={"Authorization": "Bearer owner"},
            )[2]
        )["data"]
        self.assertEqual([], page["content"])

    def test_AC_SYS_007_document_metadata_reports_first_body_field(self):
        document_id = self._upload()["documentId"]
        status, _, raw = _call(
            self.app,
            "PATCH",
            f"/api/documents/{document_id}",
            headers={
                "Authorization": "Bearer owner",
                "Content-Type": "application/json",
            },
            body=b'{"title":7,"description":8}',
        )

        error = json.loads(raw)
        self.assertEqual((400, "COMMON-002"), (status, error["code"]))
        self.assertEqual("title: 필수 문자열이며 공백일 수 없습니다.", error["message"])
        self.assertNotIn("description", error["message"])


if __name__ == "__main__":
    unittest.main()
