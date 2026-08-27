"""Chunking and embedding attempt operations."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime

from src.shared import ChunkRecord

from .model import ModelRow, OperationResult, VectorRow
from .rules import fail


class ProcessingMixin:
    def save_chunks(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: int,
        claim_token: str,
        *,
        chunks: Iterable[object] | None = None,
        creator: Callable[[], Iterable[object]] | None = None,
        now: datetime | None = None,
    ) -> OperationResult:
        moment = self._now(now)
        with self.document_transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            self._ownership(job, worker_id, claim_token, moment)
            self._started_attempt(attempt_id, job, worker_id, claim_token)
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                raise KeyError(job.document_version_id)
            document = self._store.lock_document(version.document_id)
            if document is None:
                raise KeyError(version.document_id)
            existing = self._store.get_chunks(version.id)
            if version.status == "CHUNKED" and existing:
                if self._document_ledger is not None:
                    self._document_ledger.save_chunks(version.id, existing)
                return OperationResult(200, self._chunk_data(version.id, existing))
            if version.status not in {"UPLOADED", "PARSING"}:
                fail("DOCUMENT-VERSION-005")
            if version.status == "UPLOADED":
                version.status = "PARSING"
                self._event(job.id, "PARSE_STARTED", moment)
                self._store.save_version(version)
            self._commit_document_progress(job, version, moment)
        produced = creator() if creator is not None else chunks
        if produced is None:
            fail("DOCUMENT-PARSING-002")
        converted = self._convert_chunks(job.document_version_id, produced)
        if not converted:
            fail("DOCUMENT-PARSING-002")
        with self.document_transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            self._ownership(job, worker_id, claim_token, self._now(now))
            self._started_attempt(attempt_id, job, worker_id, claim_token)
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                raise KeyError(job.document_version_id)
            existing = self._store.get_chunks(version.id)
            if version.status == "CHUNKED" and existing:
                if self._document_ledger is not None:
                    self._document_ledger.save_chunks(version.id, existing)
                return OperationResult(200, self._chunk_data(version.id, existing))
            if version.status != "PARSING":
                fail("DOCUMENT-VERSION-005")
            self._store.save_chunks(version.id, converted)
            version.status = "CHUNKED"
            self._store.save_version(version)
            self._event(job.id, "CHUNKED", moment)
            if self._document_ledger is not None:
                self._document_ledger.save_chunks(version.id, converted)
            return OperationResult(201, self._chunk_data(version.id, converted))

    def save_embeddings(
        self,
        job_id: int,
        attempt_id: int,
        worker_id: int,
        claim_token: str,
        embedder: Callable[[tuple[ChunkRecord, ...], ModelRow], Iterable[Sequence[float]]],
        now: datetime | None = None,
    ) -> OperationResult:
        moment = self._now(now)
        with self.document_transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            self._ownership(job, worker_id, claim_token, moment)
            self._started_attempt(attempt_id, job, worker_id, claim_token)
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                raise KeyError(job.document_version_id)
            document = self._store.lock_document(version.document_id)
            if document is None:
                raise KeyError(version.document_id)
            model = self._active_model()
            chunks = self._store.get_chunks(version.id)
            existing = self._vectors(version.id, model.id)
            if chunks and len(existing) == len(chunks):
                # A retry that inherits complete vectors still has to leave the
                # version and its rows in the state complete() demands; otherwise
                # the job can never reach INDEXED.  _final_failure staled these
                # rows, so a retry has to undo that, exactly as a fresh embedding
                # would insert them ACTIVE.  A deleted document keeps its rows
                # staled: reviving them would resurrect deleted content.
                if document.deleted_at is None and document.status != "DELETED":
                    for row in existing:
                        if row.status == "STALE":
                            row.status = "ACTIVE"
                            self._store.save_vector(row)
                if version.status == "CHUNKED":
                    version.status = "EMBEDDING"
                    self._event(job.id, "EMBEDDING_STARTED", moment)
                    self._store.save_version(version)
                self._commit_document_progress(job, version, moment)
                return OperationResult(200, self._embedding_data(version.id, model.id, existing))
            if version.status not in {"CHUNKED", "EMBEDDING"}:
                fail("DOCUMENT-VERSION-006")
            if not chunks:
                fail("DOCUMENT-CHUNK-001")
            if version.status == "CHUNKED":
                version.status = "EMBEDDING"
                self._event(job.id, "EMBEDDING_STARTED", moment)
                self._store.save_version(version)
            self._commit_document_progress(job, version, moment)
            chunk_snapshot = tuple(chunks)
            model_snapshot = (model.id, model.name, model.dimension)
        raw_vectors = tuple(embedder(chunk_snapshot, model))
        vectors = tuple(tuple(float(value) for value in vector) for vector in raw_vectors)
        if len(vectors) != len(chunk_snapshot) or any(
            len(vector) != model_snapshot[2]
            or any(not math.isfinite(value) for value in vector)
            for vector in vectors
        ):
            fail("DOCUMENT-EMBEDDING-002")
        with self._store.transaction():
            job = self._store.lock_job(job_id)
            if job is None:
                fail("EMBEDDING-JOB-001")
            self._ownership(job, worker_id, claim_token, self._now(now))
            self._started_attempt(attempt_id, job, worker_id, claim_token)
            version = self._store.lock_version(job.document_version_id)
            if version is None:
                raise KeyError(job.document_version_id)
            model = self._active_model()
            if (model.id, model.name, model.dimension) != model_snapshot:
                fail("DOCUMENT-EMBEDDING-001")
            if self._store.get_chunks(version.id) != chunk_snapshot:
                fail("DOCUMENT-EMBEDDING-001")
            existing = self._vectors(version.id, model.id)
            if len(existing) == len(chunk_snapshot):
                return OperationResult(200, self._embedding_data(version.id, model.id, existing))
            if existing:
                fail("DOCUMENT-EMBEDDING-001")
            rows = tuple(
                VectorRow(
                    self._id("vector"), version.id, index, model.id, vector, "ACTIVE"
                )
                for index, vector in enumerate(vectors)
            )
            self._store.insert_vectors(rows)
            return OperationResult(201, self._embedding_data(version.id, model.id, rows))
