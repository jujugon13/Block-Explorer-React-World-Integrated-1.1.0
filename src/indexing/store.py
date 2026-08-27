"""Default in-memory persistence for the indexing state machine."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import RLock

from src.shared import ChunkRecord

from .model import (
    AttemptRow,
    DocumentRow,
    EventRow,
    IndexingState,
    JobRow,
    ModelRow,
    VectorRow,
    VersionRow,
    WorkerRow,
)


class InMemoryIndexingStore:
    """Live-row store preserving the original single-process behavior."""

    def __init__(self) -> None:
        self.state = IndexingState()
        self.lock = RLock()
        self.next_ids = {
            "worker": 1,
            "document": 1,
            "version": 1,
            "job": 1,
            "attempt": 1,
            "event": 1,
            "model": 1,
            "vector": 1,
        }

    @contextmanager
    def transaction(self):
        with self.lock:
            state_before = deepcopy(self.state)
            ids_before = dict(self.next_ids)
            try:
                yield
            except Exception:
                self.state = state_before
                self.next_ids = ids_before
                raise

    @contextmanager
    def read(self):
        with self.lock:
            yield

    def next_id(self, kind: str, requested: int | None = None) -> int:
        if requested is not None:
            self.next_ids[kind] = max(self.next_ids[kind], requested + 1)
            return requested
        value = self.next_ids[kind]
        self.next_ids[kind] += 1
        return value

    def lock_worker_registration(self, instance_id: str) -> None:
        del instance_id

    def get_worker(self, worker_id: int) -> WorkerRow | None:
        return self.state.workers.get(worker_id)

    lock_worker = get_worker

    def list_workers(self) -> tuple[WorkerRow, ...]:
        return tuple(self.state.workers.values())

    def insert_worker(self, worker: WorkerRow) -> None:
        self.state.workers[worker.id] = worker

    save_worker = insert_worker

    def get_document(self, document_id: int) -> DocumentRow | None:
        return self.state.documents.get(document_id)

    lock_document = get_document

    def list_documents(self) -> tuple[DocumentRow, ...]:
        return tuple(self.state.documents.values())

    def insert_document(self, document: DocumentRow) -> None:
        self.state.documents[document.id] = document

    save_document = insert_document

    def get_version(self, version_id: int) -> VersionRow | None:
        return self.state.versions.get(version_id)

    lock_version = get_version

    def list_versions(self) -> tuple[VersionRow, ...]:
        return tuple(self.state.versions.values())

    def insert_version(self, version: VersionRow) -> None:
        self.state.versions[version.id] = version

    save_version = insert_version

    def get_job(self, job_id: int) -> JobRow | None:
        return self.state.jobs.get(job_id)

    lock_job = get_job

    def list_jobs(self) -> tuple[JobRow, ...]:
        return tuple(self.state.jobs.values())

    def lock_next_pending_job(self, now: datetime) -> JobRow | None:
        candidates = sorted(
            (
                job
                for job in self.state.jobs.values()
                if job.status == "PENDING"
                and (job.next_run_at is None or job.next_run_at <= now)
            ),
            key=lambda job: (-job.priority, job.created_at, job.id),
        )
        return candidates[0] if candidates else None

    def expired_job_ids(self, cutoff: datetime, batch_size: int) -> tuple[int, ...]:
        return tuple(
            job.id
            for job in sorted(
                (
                    job
                    for job in self.state.jobs.values()
                    if job.status == "PROCESSING"
                    and job.lease_expires_at is not None
                    and job.lease_expires_at <= cutoff
                ),
                key=lambda job: (job.lease_expires_at, job.id),
            )[:batch_size]
        )

    def insert_job(self, job: JobRow) -> None:
        self.state.jobs[job.id] = job

    save_job = insert_job

    def get_attempt(self, attempt_id: int) -> AttemptRow | None:
        return self.state.attempts.get(attempt_id)

    lock_attempt = get_attempt

    def list_attempts(self) -> tuple[AttemptRow, ...]:
        return tuple(self.state.attempts.values())

    def insert_attempt(self, attempt: AttemptRow) -> None:
        self.state.attempts[attempt.id] = attempt

    save_attempt = insert_attempt

    def list_events(self) -> tuple[EventRow, ...]:
        return tuple(self.state.events)

    def insert_event(self, event: EventRow) -> None:
        self.state.events.append(event)

    def get_model(self, model_id: int) -> ModelRow | None:
        return self.state.models.get(model_id)

    def list_models(self) -> tuple[ModelRow, ...]:
        return tuple(self.state.models.values())

    def insert_model(self, model: ModelRow) -> None:
        self.state.models[model.id] = model

    save_model = insert_model

    def get_chunks(self, version_id: int) -> tuple[ChunkRecord, ...]:
        return self.state.chunks.get(version_id, ())

    def save_chunks(self, version_id: int, chunks: tuple[ChunkRecord, ...]) -> None:
        self.state.chunks[version_id] = chunks

    def list_vectors(self) -> tuple[VectorRow, ...]:
        return tuple(self.state.vectors)

    def insert_vectors(self, vectors: tuple[VectorRow, ...]) -> None:
        self.state.vectors.extend(vectors)

    def save_vector(self, vector: VectorRow) -> None:
        for index, current in enumerate(self.state.vectors):
            if current.id == vector.id:
                self.state.vectors[index] = vector
                return
        self.state.vectors.append(vector)
