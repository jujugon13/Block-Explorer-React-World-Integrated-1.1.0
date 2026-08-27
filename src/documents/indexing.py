"""Document-owned commits exposed through the shared indexing boundary."""

from __future__ import annotations

import hashlib
from datetime import datetime

from src.shared import ChunkRecord, Identifier

from .model import StoredChunk
from .validation import fail


class DocumentIndexLedgerMixin:
    """Atomic chunk and terminal-state writes for ``DocumentWorkspace``."""

    def commit_index_progress(
        self,
        *,
        job_id: Identifier,
        version_id: Identifier,
        document_id: Identifier,
        changed_at: datetime,
        job_status: str,
        version_status: str,
        document_status: str,
    ) -> None:
        try:
            job_key = int(job_id)
            version_key = int(version_id)
            document_key = int(document_id)
        except (TypeError, ValueError):
            fail("DOCUMENT-INDEXING-003")
        if (
            job_status not in {"PENDING", "PROCESSING", "INDEXED", "FAILED"}
            or version_status
            not in {"UPLOADED", "PARSING", "CHUNKED", "EMBEDDING", "INDEXED", "FAILED"}
            or document_status not in {"UPLOADED", "INDEXING", "INDEXED", "FAILED"}
        ):
            fail("DOCUMENT-INDEXING-003")
        with self._transaction(enlist_outbox=False):
            self._store.lock_job(job_key)
            self._store.lock_version(version_key)
            self._store.lock_document(document_key)
            job = self._store.job(job_key)
            version = self._store.version(version_key)
            document = self._store.document(document_key)
            if (
                job is None
                or version is None
                or document is None
                or job.document_version_id != version_key
                or version.document_id != document_key
                or document.latest_version_id != version_key
                or document.deleted_at is not None
            ):
                fail("DOCUMENT-INDEXING-003")
            job.status = job_status
            version.status = version_status
            if version_status != "INDEXED":
                version.indexed_at = None
            document.status = document_status
            document.updated_at = changed_at
            self._store.save_job(job)
            self._store.save_version(version)
            self._store.save_document(document)

    def save_chunks(
        self, version_id: Identifier, chunks: tuple[ChunkRecord, ...]
    ) -> None:
        try:
            key = int(version_id)
        except (TypeError, ValueError):
            fail("DOCUMENT-VERSION-005")
        converted: list[StoredChunk] = []
        if not chunks:
            fail("DOCUMENT-PARSING-002")
        for expected, item in enumerate(chunks):
            try:
                item_version_id = int(item.version_id)
            except (TypeError, ValueError):
                fail("DOCUMENT-CHUNK-001")
            text = item.text
            if (
                item_version_id != key
                or item.index != expected
                or item.start < 0
                or item.end <= item.start
                or item.end - item.start != len(text)
                or item.text_sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest()
                or item.token_estimate < 0
            ):
                fail("DOCUMENT-CHUNK-001")
            converted.append(
                StoredChunk(
                    item.index,
                    item.start,
                    item.end,
                    text,
                    item.text_sha256,
                    item.token_estimate,
                    item.page_number,
                    item.section_title,
                )
            )
        with self._transaction(enlist_outbox=False):
            self._store.lock_version(key)
            version = self._store.version(key)
            if version is None:
                fail("DOCUMENT-VERSION-005")
            self._store.lock_document(version.document_id)
            document = self._store.document(version.document_id)
            if document is None or document.status == "DELETED":
                fail("DOCUMENT-VERSION-005")
            existing = self._store.chunks(key)
            if existing is not None:
                if existing == tuple(converted):
                    return
                fail("DOCUMENT-CHUNK-001")
            if version.status not in {"UPLOADED", "PARSING"}:
                fail("DOCUMENT-VERSION-005")
            inserted, concurrent = self._store.insert_chunks_if_absent(
                key, tuple(converted)
            )
            if not inserted:
                if concurrent == tuple(converted):
                    return
                fail("DOCUMENT-CHUNK-001")
            version.status = "CHUNKED"
            self._store.save_version(version)

    def commit_index_result(
        self,
        *,
        job_id: Identifier,
        attempt_id: Identifier,
        version_id: Identifier,
        document_id: Identifier,
        indexed_at: datetime,
    ) -> None:
        job_key = int(job_id)
        attempt_key = int(attempt_id)
        version_key = int(version_id)
        document_key = int(document_id)
        if attempt_key < 1:
            fail("DOCUMENT-INDEXING-003")
        with self._transaction(enlist_outbox=False):
            self._store.lock_job(job_key)
            self._store.lock_version(version_key)
            self._store.lock_document(document_key)
            job = self._store.job(job_key)
            version = self._store.version(version_key)
            document = self._store.document(document_key)
            if (
                job is None
                or version is None
                or document is None
                or job.document_version_id != version_key
                or version.document_id != document_key
                or document.latest_version_id != version_key
                or document.deleted_at is not None
            ):
                fail("DOCUMENT-INDEXING-003")
            if (
                job.status == "INDEXED"
                and version.status == "INDEXED"
                and version.indexed_at == indexed_at
                and document.status == "INDEXED"
                and document.current_version_id == version_key
            ):
                return
            if job.status not in {"PENDING", "PROCESSING"} or version.status not in {
                "CHUNKED",
                "EMBEDDING",
            }:
                fail("DOCUMENT-INDEXING-003")
            job.status = "INDEXED"
            version.status = "INDEXED"
            version.indexed_at = indexed_at
            document.status = "INDEXED"
            document.current_version_id = version_key
            document.updated_at = indexed_at
            self._store.save_job(job)
            self._store.save_version(version)
            self._store.save_document(document)

    def commit_index_failure(
        self,
        *,
        job_id: Identifier,
        attempt_id: Identifier,
        version_id: Identifier,
        document_id: Identifier,
        failed_at: datetime,
        job_status: str,
        version_status: str,
        document_status: str,
    ) -> None:
        job_key = int(job_id)
        attempt_key = int(attempt_id)
        version_key = int(version_id)
        document_key = int(document_id)
        if (
            attempt_key < 1
            or job_status not in {"PENDING", "FAILED"}
            or (job_status == "FAILED" and version_status != "FAILED")
        ):
            fail("DOCUMENT-INDEXING-004")
        with self._transaction(enlist_outbox=False):
            self._store.lock_job(job_key)
            self._store.lock_version(version_key)
            self._store.lock_document(document_key)
            job = self._store.job(job_key)
            version = self._store.version(version_key)
            document = self._store.document(document_key)
            if (
                job is None
                or version is None
                or document is None
                or job.document_version_id != version_key
                or version.document_id != document_key
                or document.deleted_at is not None
            ):
                fail("DOCUMENT-INDEXING-004")
            job.status = job_status
            version.status = version_status
            if version_status != "INDEXED":
                version.indexed_at = None
            document.status = document_status
            document.updated_at = failed_at
            self._store.save_job(job)
            self._store.save_version(version)
            self._store.save_document(document)
