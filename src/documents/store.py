"""Default in-memory implementation of the shared document-ledger port."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from .model import DocumentState


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self.state = DocumentState()
        self.next_ids = {
            "file": 1,
            "document": 1,
            "version": 1,
            "job": 1,
            "event": 1,
        }
        self._lock = RLock()

    @contextmanager
    def read(self):
        with self._lock:
            yield

    @contextmanager
    def transaction(self):
        with self._lock:
            state_before = deepcopy(self.state)
            ids_before = dict(self.next_ids)
            try:
                yield
            except Exception:
                self.state = state_before
                self.next_ids = ids_before
                raise

    def next_id(self, kind: str) -> int:
        value = self.next_ids[kind]
        self.next_ids[kind] += 1
        return value

    def lock_job(self, job_id: int) -> None:
        del job_id

    def lock_version(self, version_id: int) -> None:
        del version_id

    def lock_document(self, document_id: int) -> None:
        del document_id

    def find_file(self, digest: str, size: int):
        file_id = self.state.file_by_digest_size.get((digest, size))
        return self.state.files.get(file_id) if file_id is not None else None

    def file(self, file_id: int):
        return self.state.files.get(file_id)

    def insert_file_if_absent(self, row):
        if self.find_file(row.digest, row.size) is not None:
            return False
        self.state.files[row.id] = row
        self.state.file_by_digest_size[(row.digest, row.size)] = row.id
        return True

    def document(self, document_id: int):
        return self.state.documents.get(document_id)

    def documents(self):
        return tuple(self.state.documents.values())

    def insert_document(self, row) -> None:
        self.state.documents[row.id] = row

    def save_document(self, row) -> None:
        self.state.documents[row.id] = row

    def version(self, version_id: int):
        return self.state.versions.get(version_id)

    def versions(self, document_id: int):
        version_ids = self.state.version_ids_by_document[document_id]
        return tuple(
            self.state.versions[item] for item in version_ids
        )

    def insert_version(self, row) -> None:
        self.state.versions[row.id] = row
        self.state.version_ids_by_document.setdefault(row.document_id, []).append(row.id)

    def save_version(self, row) -> None:
        self.state.versions[row.id] = row

    def job(self, job_id: int):
        return self.state.jobs.get(job_id)

    def job_for_version(self, version_id: int):
        job_id = self.state.job_id_by_version.get(version_id)
        return self.state.jobs.get(job_id) if job_id is not None else None

    def insert_job(self, row) -> None:
        self.state.jobs[row.id] = row
        self.state.job_id_by_version[row.document_version_id] = row.id

    def save_job(self, row) -> None:
        self.state.jobs[row.id] = row

    def chunks(self, version_id: int):
        rows = self.state.chunks_by_version.get(version_id)
        return tuple(rows) if rows is not None else None

    def insert_chunks_if_absent(self, version_id: int, rows: tuple):
        existing = self.chunks(version_id)
        if version_id in self.state.chunks_by_version:
            return False, existing or ()
        self.state.chunks_by_version[version_id] = list(rows)
        return True, ()

    def replace_chunks(self, version_id: int, rows: tuple) -> None:
        self.state.chunks_by_version[version_id] = list(rows)

    def append_event(self, row) -> None:
        self.state.events.append(row)

    def has_documents(self) -> bool:
        return bool(self.state.documents)

    def compatibility_state(self):
        return self.state

    def compatibility_next_ids(self) -> dict[str, int]:
        return self.next_ids
