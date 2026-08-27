"""Document upload, versioning, query, and logical-deletion behavior."""

from __future__ import annotations

import logging
from contextlib import contextmanager, nullcontext
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from uuid import uuid4

from src.shared import (
    DocumentIndexParticipant,
    DocumentSyncOutbox,
    DocumentVersionRegistration,
    ObjectStorage,
    Principal,
)
from src.shared.document_ledger import CommitOutcomeUnknown, DocumentLedgerStore
from .indexing import DocumentIndexLedgerMixin
from .binding import DocumentBindingMixin

from .model import (
    Document,
    DocumentState,
    DocumentVersion,
    FileObject,
    IndexJob,
    OperationResult,
    OutboxEvent,
    StoredChunk,
    UploadFile,
    ValidatedFile,
)
from .query.flow import DocumentQueries
from .store import InMemoryDocumentStore
from .validation import (
    ACTIVE_VERSION_STATES,
    EDIT_VISIBILITIES,
    UPLOAD_VISIBILITIES,
    fail,
    normalize_description,
    normalize_title,
    validate_file,
)

LOGGER = logging.getLogger(__name__)
AccessDecider = Callable[[Principal, Document, str], bool]


class DocumentWorkspace(DocumentBindingMixin, DocumentIndexLedgerMixin, DocumentQueries):
    """In-process relational ledger with explicit storage I/O boundaries."""

    def __init__(
        self,
        storage: ObjectStorage,
        *,
        access_decider: AccessDecider | None = None,
        indexing: DocumentIndexParticipant | None = None,
        sync_outbox: DocumentSyncOutbox | None = None,
        clock: Callable[[], datetime] | None = None,
        store: DocumentLedgerStore | None = None,
    ) -> None:
        self.storage = storage
        self._store = store if store is not None else InMemoryDocumentStore()
        self._access_decider = access_decider or self._default_access
        self._indexing = indexing
        self._sync_outbox = sync_outbox
        self._clock = clock or (lambda: datetime.now(UTC))
        self.fail_next_commit = False
        if self._indexing is not None:
            self._indexing.bind_document_ledger(self)

    @property
    def state(self) -> DocumentState:
        return self._store.compatibility_state()

    @property
    def _next_ids(self) -> dict[str, int]:
        return self._store.compatibility_next_ids()
    def _read(self):
        return self._store.read()
    @staticmethod
    def _default_access(principal: Principal, document: Document, action: str) -> bool:
        if principal.user_id == document.owner_user_id:
            return True
        return action == "READ" and document.visibility == "PUBLIC"

    def _id(self, kind: str) -> int:
        return self._store.next_id(kind)

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @contextmanager
    def _transaction(self, *, enlist_outbox: bool = True):
        """Commit the document ledger and durable sync events as one unit."""
        with self._store.transaction():
            outbox_transaction = (
                self._sync_outbox.transaction()
                if self._sync_outbox is not None and enlist_outbox
                else nullcontext()
            )
            with outbox_transaction:
                yield

    @contextmanager
    def _creation_transaction(self):
        indexing_transaction = (
            self._indexing.document_transaction()
            if self._indexing is not None
            else nullcontext()
        )
        with indexing_transaction:
            with self._transaction():
                yield

    def _register_index_version(
        self,
        document: Document,
        version: DocumentVersion,
        job: IndexJob,
        created_at: datetime,
    ) -> None:
        if self._indexing is None:
            return
        self._indexing.register_document_version(
            DocumentVersionRegistration(
                document.id,
                document.status,
                document.current_version_id,
                document.latest_version_id,
                document.deleted_at,
                version.id,
                version.version_no,
                version.status,
                job.id,
                created_at,
            )
        )

    def _require(self, principal: Principal, document: Document, action: str) -> None:
        if not self._access_decider(principal, document, action):
            fail("ROLE-002")

    def _file_for(self, validated: ValidatedFile) -> FileObject | None:
        with self._read():
            return self._store.find_file(validated.digest, validated.size)

    def _candidate(self, validated: ValidatedFile) -> FileObject:
        key = f"documents/{uuid4()}/{uuid4()}.{validated.extension}"
        location = self.storage.put(key, validated.data, validated.size)
        return FileObject(
            0,
            validated.digest,
            validated.size,
            validated.filename,
            validated.content_type,
            validated.document_type,
            location,
        )

    def _cleanup(self, candidate: FileObject | None) -> None:
        if candidate is None:
            return
        try:
            self.storage.delete(candidate.location)
        except Exception as error:
            LOGGER.warning("candidate_cleanup_failed error_type=%s", type(error).__name__)

    def _adopt_file(self, candidate: FileObject) -> tuple[FileObject, bool]:
        existing = self._store.find_file(candidate.digest, candidate.size)
        if existing is not None:
            return existing, False
        candidate.id = self._id("file")
        if self._store.insert_file_if_absent(candidate):
            return candidate, True
        existing = self._store.find_file(candidate.digest, candidate.size)
        if existing is None:
            raise RuntimeError("file adoption failed")
        return existing, False

    def _new_job_and_event(self, version_id: int, event_type: str, now: datetime) -> tuple[IndexJob, OutboxEvent]:
        job = IndexJob(self._id("job"), version_id, now)
        event = OutboxEvent(self._id("event"), event_type, version_id)
        self._store.insert_job(job)
        self._store.append_event(event)
        return job, event

    def upload(
        self,
        principal: Principal,
        upload: UploadFile,
        *,
        title: str,
        description: str | None,
        visibility: str,
    ) -> OperationResult:
        validated = validate_file(upload)
        normalized_title = normalize_title(title)
        normalized_description = normalize_description(description)
        if visibility not in UPLOAD_VISIBILITIES:
            fail("COMMON-002")

        existing = self._file_for(validated)
        candidate: FileObject | None = None
        if existing is not None:
            self.storage.ensure_location(existing.location)
        else:
            candidate = self._candidate(validated)

        adopted = False
        try:
            with self._creation_transaction():
                if self.fail_next_commit:
                    self.fail_next_commit = False
                    raise RuntimeError("forced ledger failure")
                file_object, adopted = self._adopt_file(candidate) if candidate else (existing, False)
                if file_object is None:
                    raise RuntimeError("file adoption failed")
                now = self._now()
                document_id = self._id("document")
                version_id = self._id("version")
                document = Document(
                    document_id,
                    normalized_title,
                    normalized_description,
                    validated.document_type,
                    "UPLOAD",
                    "UPLOADED",
                    visibility,
                    principal.user_id if principal.user_id is not None else 0,
                    principal.display_name or principal.subject,
                    None,
                    version_id,
                    now,
                    now,
                )
                version = DocumentVersion(
                    version_id,
                    document_id,
                    1,
                    file_object.id,
                    normalized_title,
                    "UPLOADED",
                    now,
                )
                self._store.insert_document(document)
                self._store.insert_version(version)
                job, _ = self._new_job_and_event(version_id, "DOCUMENT_VERSION_CREATED", now)
                self._register_index_version(document, version, job, now)
                if self._sync_outbox is not None:
                    self._sync_outbox.publish_document_version_created(
                        version_id,
                        1,
                        payload={"documentId": document_id, "versionId": version_id},
                        occurred_at=now,
                    )
        except CommitOutcomeUnknown:
            raise
        except Exception:
            if candidate is not None:
                self._cleanup(candidate)
            raise
        if candidate is not None and not adopted:
            self._cleanup(candidate)
        return OperationResult(
            201,
            {
                "documentId": document.id,
                "documentVersionId": version.id,
                "fileObjectId": file_object.id,
                "embeddingJobId": job.id,
                "documentStatus": document.status,
                "jobStatus": job.status,
            },
        )

    def _validate_new_version(
        self, document_id: int, owner_id: int | None, validated: ValidatedFile
    ) -> tuple[Document, DocumentVersion]:
        document = self._store.document(document_id)
        if document is None:
            fail("DOCUMENT-001")
        if owner_id != document.owner_user_id:
            fail("ROLE-002")
        if document.status == "DELETED":
            fail("DOCUMENT-001")
        if document.source_type != "UPLOAD":
            fail("DOCUMENT-VERSION-003")
        if document.document_type != validated.document_type:
            fail("DOCUMENT-VERSION-004")
        versions = list(self._store.versions(document.id))
        if any(version.status in ACTIVE_VERSION_STATES for version in versions):
            fail("DOCUMENT-VERSION-002")
        if document.status == "INDEXED":
            comparison = self._store.version(document.current_version_id or -1)
            if comparison is None or comparison.status != "INDEXED":
                fail("DOCUMENT-VERSION-003")
        elif document.status == "FAILED":
            comparison = max(versions, key=lambda item: item.version_no)
            if comparison.status != "FAILED":
                fail("DOCUMENT-VERSION-003")
        else:
            fail("DOCUMENT-VERSION-003")
        compared_file = self._store.file(comparison.file_object_id)
        if compared_file is None:
            raise KeyError(comparison.file_object_id)
        if compared_file.digest == validated.digest and compared_file.size == validated.size:
            fail("DOCUMENT-VERSION-001")
        return document, comparison

    def add_version(
        self, principal: Principal, document_id: int, upload: UploadFile
    ) -> OperationResult:
        validated = validate_file(upload)
        with self._read():
            self._validate_new_version(document_id, principal.user_id, validated)
        existing = self._file_for(validated)
        candidate: FileObject | None = None
        if existing is not None:
            self.storage.ensure_location(existing.location)
        else:
            candidate = self._candidate(validated)

        adopted = False
        try:
            with self._creation_transaction():
                self._store.lock_document(document_id)
                document, _ = self._validate_new_version(document_id, principal.user_id, validated)
                if self.fail_next_commit:
                    self.fail_next_commit = False
                    raise RuntimeError("forced ledger failure")
                file_object, adopted = self._adopt_file(candidate) if candidate else (existing, False)
                if file_object is None:
                    raise RuntimeError("file adoption failed")
                old_current = document.current_version_id
                version_no = max(
                    item.version_no for item in self._store.versions(document.id)
                ) + 1
                now = self._now()
                version = DocumentVersion(
                    self._id("version"),
                    document.id,
                    version_no,
                    file_object.id,
                    document.title,
                    "UPLOADED",
                    now,
                )
                self._store.insert_version(version)
                document.latest_version_id = version.id
                if document.status == "FAILED":
                    document.status = "UPLOADED"
                document.updated_at = now
                self._store.save_document(document)
                job, _ = self._new_job_and_event(version.id, "DOCUMENT_VERSION_CREATED", now)
                self._register_index_version(document, version, job, now)
                if self._sync_outbox is not None:
                    self._sync_outbox.publish_document_version_created(
                        version.id,
                        version.version_no,
                        payload={"documentId": document.id, "versionId": version.id},
                        occurred_at=now,
                    )
        except CommitOutcomeUnknown:
            raise
        except Exception:
            if candidate is not None:
                self._cleanup(candidate)
            raise
        if candidate is not None and not adopted:
            self._cleanup(candidate)
        return OperationResult(
            201,
            {
                "documentId": document.id,
                "documentVersionId": version.id,
                "versionNo": version.version_no,
                "embeddingJobId": job.id,
                "currentVersionId": old_current,
                "documentStatus": document.status,
                "versionStatus": version.status,
                "jobStatus": job.status,
            },
        )

    def update_metadata(
        self, principal: Principal, document_id: int, *, title: str, description: str | None
    ) -> None:
        normalized_title = normalize_title(title)
        normalized_description = normalize_description(description)
        with self._read():
            document = self._visible_document(document_id)
        self._require(principal, document, "WRITE")
        with self._transaction(enlist_outbox=False):
            self._store.lock_document(document_id)
            document = self._visible_document(document_id)
            document.title = normalized_title
            document.description = normalized_description
            document.updated_at = self._now()
            self._store.save_document(document)

    def update_visibility(self, principal: Principal, document_id: int, visibility: str) -> None:
        with self._transaction(enlist_outbox=False):
            self._store.lock_document(document_id)
            document = self._visible_document(document_id)
            if principal.user_id != document.owner_user_id:
                fail("ROLE-002")
            if visibility not in EDIT_VISIBILITIES:
                fail("DOCUMENT-VISIBILITY-001")
            document.visibility = visibility
            document.updated_at = self._now()
            self._store.save_document(document)

    def delete(self, principal: Principal, document_id: int) -> None:
        with self._read():
            document = self._visible_document(document_id)
        self._require(principal, document, "ADMIN")
        with self._transaction():
            self._store.lock_document(document_id)
            document = self._visible_document(document_id)
            now = self._now()
            document.status = "DELETED"
            document.deleted_at = now
            document.updated_at = now
            self._store.save_document(document)
            self._store.append_event(
                OutboxEvent(self._id("event"), "DOCUMENT_DELETED", document.id)
            )
            if self._sync_outbox is not None:
                self._sync_outbox.publish_document_deleted(
                    document.id,
                    payload={"documentId": document.id},
                    occurred_at=now,
                )

    def _visible_document(self, document_id: int) -> Document:
        document = self._store.document(document_id)
        if document is None or document.status == "DELETED":
            fail("DOCUMENT-001")
        return document

    def set_version_state(self, document_id: int, state: str) -> DocumentVersion:
        with self._read():
            document = self._store.document(document_id)
            if document is None:
                raise KeyError(document_id)
            version_id = document.latest_version_id
        with self._transaction(enlist_outbox=False):
            self._store.lock_version(version_id)
            self._store.lock_document(document_id)
            document = self._store.document(document_id)
            version = self._store.version(version_id)
            if document is None:
                raise KeyError(document_id)
            if version is None:
                raise KeyError(version_id)
            version.status = state
            if state == "INDEXED":
                now = self._now()
                version.indexed_at = now
                document.current_version_id = version.id
                document.status = "INDEXED"
            elif state == "FAILED":
                document.status = "INDEXED" if document.current_version_id else "FAILED"
            self._store.save_version(version)
            self._store.save_document(document)
            return version

    def put_chunks(self, version_id: int, chunks: Iterable[object]) -> None:
        converted = [
            StoredChunk(
                int(item.index),
                int(item.start),
                int(item.end),
                str(item.text),
                str(item.text_sha256),
                int(item.token_estimate),
                getattr(item, "page_number", None),
                getattr(item, "section_title", None),
            )
            for item in chunks
        ]
        with self._transaction(enlist_outbox=False):
            self._store.lock_version(version_id)
            version = self._store.version(version_id)
            if version is None:
                raise KeyError(version_id)
            self._store.replace_chunks(version_id, tuple(converted))
            version.status = "CHUNKED"
            self._store.save_version(version)

    def versions(self, document_id: int) -> tuple[DocumentVersion, ...]:
        with self._read():
            return tuple(self._store.versions(document_id))
