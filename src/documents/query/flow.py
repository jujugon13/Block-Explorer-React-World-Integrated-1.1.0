from __future__ import annotations

import hashlib
import math
from datetime import datetime
from urllib.parse import quote

from src.shared import (
    ChunkRecord,
    Identifier,
    OpsDocumentSnapshot,
    Principal,
    ResourceAccess,
    Response,
    resolve_document_search_id,
    VersionFileSnapshot,
)

from ..model import Document, DocumentVersion
from ..validation import ACTIVE_VERSION_STATES, fail, validate_page


class DocumentQueries:
    """Read paths; the concrete workspace supplies the ledger lock and access policy."""

    def document_access(
        self, document_id: int | str, *, include_deleted: bool = False
    ) -> ResourceAccess | None:
        with self._read():
            key = resolve_document_search_id(document_id)
            if key is None:
                return None
            document = self._store.document(key)
            if document is None or (document.status == "DELETED" and not include_deleted):
                return None
            return ResourceAccess(
                document.id,
                document.owner_user_id,
                document.visibility,
                document.status,
            )

    def document_ids(self) -> frozenset[int]:
        with self._read():
            return frozenset(
                document.id
                for document in self._store.documents()
                if document.status != "DELETED"
            )

    def snapshot_for_chunking(self, version_id: Identifier) -> VersionFileSnapshot:
        try:
            key = int(version_id)
        except (TypeError, ValueError):
            fail("DOCUMENT-VERSION-005")
        with self._read():
            version = self._store.version(key)
            document = (
                self._store.document(version.document_id)
                if version is not None
                else None
            )
            file_object = (
                self._store.file(version.file_object_id)
                if version is not None
                else None
            )
            if (
                version is None
                or document is None
                or file_object is None
                or document.deleted_at is not None
            ):
                fail("DOCUMENT-VERSION-005")
            return VersionFileSnapshot(
                document.id,
                version.id,
                version.version_no,
                version.status,
                document.status,
                document.document_type,
                file_object.location,
            )

    def chunks_for_embedding(self, version_id: Identifier) -> tuple[ChunkRecord, ...]:
        try:
            key = int(version_id)
        except (TypeError, ValueError):
            fail("DOCUMENT-VERSION-006")
        with self._read():
            version = self._store.version(key)
            if version is None or version.status not in {"CHUNKED", "EMBEDDING", "INDEXED"}:
                fail("DOCUMENT-VERSION-006")
            chunks = tuple(
                sorted(self._store.chunks(key) or (), key=lambda item: item.index)
            )
            if not chunks:
                fail("DOCUMENT-CHUNK-001")
            result = tuple(
                ChunkRecord(
                    key,
                    item.index,
                    item.start,
                    item.end,
                    item.text,
                    item.text_sha256,
                    item.token_estimate,
                    item.page_number,
                    item.section_title,
                )
                for item in chunks
            )
            if any(
                item.index != expected
                or item.start < 0
                or item.end <= item.start
                or item.end - item.start != len(item.text)
                or item.text_sha256
                != hashlib.sha256(item.text.encode("utf-8")).hexdigest()
                for expected, item in enumerate(result)
            ):
                fail("DOCUMENT-CHUNK-001")
            return result

    def ops_document_snapshots(
        self, now: datetime
    ) -> tuple[OpsDocumentSnapshot, ...]:
        del now
        with self._read():
            return tuple(
                OpsDocumentSnapshot(item.status, item.deleted_at)
                for item in sorted(self._store.documents(), key=lambda row: row.id)
            )

    def _version_payload(self, version: DocumentVersion) -> dict[str, object]:
        file_object = self._store.file(version.file_object_id)
        if file_object is None:
            raise KeyError(version.file_object_id)
        return {
            "documentVersionId": version.id,
            "versionNo": version.version_no,
            "status": version.status,
            "originalFilename": file_object.filename,
            "contentType": file_object.content_type,
            "fileSize": file_object.size,
            "indexedAt": version.indexed_at.isoformat() if version.indexed_at else None,
            "createdAt": version.created_at.isoformat(),
        }

    def _detail_payload(self, document: Document) -> dict[str, object]:
        current = (
            self._store.version(document.current_version_id)
            if document.current_version_id is not None
            else None
        )
        selected = current or self._store.version(document.latest_version_id)
        if selected is None:
            raise KeyError(document.latest_version_id)
        return {
            "documentId": document.id,
            "title": document.title,
            "description": document.description,
            "documentType": document.document_type,
            "sourceType": document.source_type,
            "status": document.status,
            "visibility": document.visibility,
            "ownerUserId": document.owner_user_id,
            "ownerName": document.owner_name,
            "currentVersion": self._version_payload(current) if current else None,
            "contentAvailable": bool(self._store.chunks(selected.id)),
            "createdAt": document.created_at.isoformat(),
            "updatedAt": document.updated_at.isoformat(),
        }

    def detail(self, principal: Principal, document_id: int) -> dict[str, object]:
        with self._read():
            document = self._visible_document(document_id)
        self._require(principal, document, "READ")
        with self._read():
            document = self._visible_document(document_id)
            return self._detail_payload(document)

    def status(self, principal: Principal, document_id: int) -> dict[str, object]:
        with self._read():
            document = self._visible_document(document_id)
        self._require(principal, document, "READ")
        with self._read():
            document = self._visible_document(document_id)
            current = (
                self._store.version(document.current_version_id)
                if document.current_version_id is not None
                else None
            )
            processing = next(
                (
                    item
                    for item in reversed(self._store.versions(document.id))
                    if item.status in ACTIVE_VERSION_STATES
                ),
                None,
            )
            processing_job = (
                self._store.job_for_version(processing.id) if processing else None
            )
            return {
                "documentId": document.id,
                "documentStatus": document.status,
                "currentVersion": (
                    {"versionNo": current.version_no, "status": current.status} if current else None
                ),
                "processingVersion": (
                    {
                        "versionNo": processing.version_no,
                        "status": processing.status,
                        "jobStatus": processing_job.status,
                    }
                    if processing and processing_job
                    else None
                ),
            }

    def content(self, principal: Principal, document_id: int) -> dict[str, object]:
        with self._read():
            document = self._visible_document(document_id)
        self._require(principal, document, "READ")
        with self._read():
            document = self._visible_document(document_id)
            version_id = document.current_version_id or document.latest_version_id
            version = self._store.version(version_id)
            if version is None:
                raise KeyError(version_id)
            chunks = sorted(self._store.chunks(version_id) or (), key=lambda item: item.index)
            if not chunks:
                fail("DOCUMENT-CONTENT-001")
            rebuilt: list[str] = []
            covered_end = 0
            for expected_index, chunk in enumerate(chunks):
                if (
                    chunk.index != expected_index
                    or chunk.end - chunk.start != len(chunk.text)
                    or hashlib.sha256(chunk.text.encode()).hexdigest() != chunk.text_sha256
                    or chunk.start < 0
                    or chunk.end <= chunk.start
                ):
                    fail("DOCUMENT-CHUNK-001")
                if expected_index == 0 and chunk.start != 0:
                    fail("DOCUMENT-CHUNK-001")
                if chunk.start > covered_end:
                    if chunk.start - covered_end != 1:
                        fail("DOCUMENT-CHUNK-001")
                    rebuilt.append("\n")
                overlap = max(0, covered_end - chunk.start)
                if overlap >= len(chunk.text) or chunk.end <= covered_end:
                    fail("DOCUMENT-CHUNK-001")
                rebuilt.append(chunk.text[overlap:])
                covered_end = chunk.end
            return {
                "documentId": document.id,
                "documentVersionId": version.id,
                "versionNo": version.version_no,
                "content": "".join(rebuilt),
                "chunkCount": len(chunks),
            }

    def file(
        self, principal: Principal, document_id: int, disposition: str | None = None
    ) -> Response:
        selected_disposition = "inline" if disposition is None else disposition
        if selected_disposition not in {"inline", "attachment"}:
            fail("COMMON-002")
        with self._read():
            document = self._visible_document(document_id)
        self._require(principal, document, "READ")
        with self._read():
            document = self._visible_document(document_id)
            version = self._store.version(document.latest_version_id)
            if version is None:
                raise KeyError(document.latest_version_id)
            file_object = self._store.file(version.file_object_id)
            if file_object is None:
                raise KeyError(version.file_object_id)
            location = file_object.location
            filename = file_object.filename
            content_type = file_object.content_type or "application/octet-stream"
            expected_size = file_object.size
        data = self.storage.get(location)
        if len(data) != expected_size:
            fail("DOCUMENT-STORAGE-001")
        return Response(
            200,
            data,
            (
                ("Content-Type", content_type),
                ("Content-Length", str(expected_size)),
                (
                    "Content-Disposition",
                    f"{selected_disposition}; filename*=UTF-8''{quote(filename)}",
                ),
                ("Cache-Control", "no-store"),
            ),
        )

    def list(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        page: int = 0,
        size: int = 20,
    ) -> dict[str, object]:
        validate_page(page, size)
        with self._read():
            documents = [
                document
                for document in self._store.documents()
                if (status is not None and document.status == status)
                or (status is None and document.status != "DELETED")
            ]
        readable_ids = {
            document.id
            for document in documents
            if self._access_decider(principal, document, "READ")
        }
        with self._read():
            documents = [
                document
                for document in self._store.documents()
                if document.id in readable_ids
                and ((status is not None and document.status == status)
                     or (status is None and document.status != "DELETED"))
            ]
            documents.sort(key=lambda item: item.id)
            total = len(documents)
            selected = documents[page * size : (page + 1) * size]
            pages = math.ceil(total / size) if total else 0
            return {
                "content": [self._detail_payload(item) for item in selected],
                "page": page,
                "size": size,
                "totalElements": total,
                "totalPages": pages,
                "first": page == 0,
                "last": page >= max(0, pages - 1),
            }
