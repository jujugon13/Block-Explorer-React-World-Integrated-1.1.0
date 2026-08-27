"""Document, version, job, and embedding-model registration."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from src.shared import ChunkRecord, DocumentVersionRegistration

from .model import DocumentRow, JobRow, ModelRow, VectorRow, VersionRow


class RegistryMixin:
    """Seed and composition operations for the indexing ledger."""

    def add_document(
        self,
        *,
        document_id: int | None = None,
        status: str = "UPLOADED",
        current_version_id: int | None = None,
        deleted_at: datetime | None = None,
    ) -> DocumentRow:
        with self._store.transaction():
            item = DocumentRow(
                self._id("document", document_id),
                status,
                current_version_id,
                current_version_id,
                deleted_at,
            )
            self._store.insert_document(item)
            return item

    def add_version(
        self,
        document_id: int,
        version_no: int,
        *,
        version_id: int | None = None,
        status: str = "UPLOADED",
        indexed_at: datetime | None = None,
        latest: bool = True,
    ) -> VersionRow:
        with self._store.transaction():
            document = self._store.lock_document(document_id)
            if document is None:
                raise KeyError(document_id)
            item = VersionRow(
                self._id("version", version_id), document_id, version_no, status, indexed_at
            )
            self._store.insert_version(item)
            if latest:
                document.latest_version_id = item.id
            if status == "INDEXED" and document.current_version_id is None:
                document.current_version_id = item.id
                document.status = "INDEXED"
            self._store.save_document(document)
            return item

    def create_job(
        self,
        version_id: int,
        *,
        job_id: int | None = None,
        priority: int | None = None,
        max_retries: int | None = None,
        retry_count: int = 0,
        status: str = "PENDING",
        created_at: datetime | None = None,
        next_run_at: datetime | None = None,
    ) -> JobRow:
        with self._store.transaction():
            if self._store.lock_version(version_id) is None:
                raise KeyError(version_id)
            item = JobRow(
                self._id("job", job_id),
                version_id,
                status,
                self.default_job_priority if priority is None else priority,
                self.default_job_max_retries if max_retries is None else max_retries,
                retry_count,
                self._now(created_at),
                next_run_at,
            )
            self._store.insert_job(item)
            return item

    def register_document_version(
        self, registration: DocumentVersionRegistration
    ) -> None:
        """Create the queue row with the exact document-owned identifiers."""

        try:
            document_id = int(registration.document_id)
            current_version_id = (
                int(registration.current_version_id)
                if registration.current_version_id is not None
                else None
            )
            latest_version_id = int(registration.latest_version_id)
            version_id = int(registration.version_id)
            job_id = int(registration.job_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid document indexing registration") from error
        if (
            min(document_id, version_id, job_id, registration.version_no) < 1
            or latest_version_id != version_id
        ):
            raise RuntimeError("invalid document indexing registration")
        with self._store.transaction():
            document = self._store.lock_document(document_id)
            version = self._store.lock_version(version_id)
            job = self._store.lock_job(job_id)
            if version is not None or job is not None:
                if (
                    document is not None
                    and version is not None
                    and job is not None
                    and document.status == registration.document_status
                    and document.current_version_id == current_version_id
                    and document.latest_version_id == latest_version_id
                    and document.deleted_at == registration.deleted_at
                    and version.document_id == document_id
                    and version.version_no == registration.version_no
                    and version.status == registration.version_status
                    and job.document_version_id == version_id
                    and job.status == "PENDING"
                    and job.created_at == self._now(registration.created_at)
                ):
                    return
                raise RuntimeError("document indexing identifier collision")
            if any(
                item.document_id == document_id
                and item.version_no == registration.version_no
                for item in self._store.list_versions()
            ):
                raise RuntimeError("duplicate document version registration")
            if document is None:
                if registration.version_no != 1 or current_version_id is not None:
                    raise RuntimeError("initial document registration is inconsistent")
            else:
                versions = tuple(
                    item
                    for item in self._store.list_versions()
                    if item.document_id == document_id
                )
                previous_latest = max(versions, key=lambda item: item.version_no, default=None)
                previous_status_matches = (
                    document.status == registration.document_status
                    or (
                        document.status == "FAILED"
                        and registration.document_status == "UPLOADED"
                    )
                )
                if (
                    document.deleted_at is not None
                    or document.current_version_id != current_version_id
                    or previous_latest is None
                    or document.latest_version_id != previous_latest.id
                    or not previous_status_matches
                    or registration.deleted_at != document.deleted_at
                    or registration.version_no
                    != 1 + max((item.version_no for item in versions), default=0)
                ):
                    raise RuntimeError("document version registration is inconsistent")

            if document is None:
                document = DocumentRow(
                    self._id("document", document_id),
                    registration.document_status,
                    current_version_id,
                    latest_version_id,
                    registration.deleted_at,
                )
                self._store.insert_document(document)
            else:
                document.status = registration.document_status
                document.current_version_id = current_version_id
                document.latest_version_id = latest_version_id
                self._store.save_document(document)
            self._store.insert_version(VersionRow(
                self._id("version", version_id),
                document_id,
                registration.version_no,
                registration.version_status,
            ))
            self._store.insert_job(JobRow(
                self._id("job", job_id),
                version_id,
                "PENDING",
                self.default_job_priority,
                self.default_job_max_retries,
                0,
                self._now(registration.created_at),
            ))

    def add_model(
        self,
        name: str = "embedding-model",
        dimension: int = 3,
        *,
        model_id: int | None = None,
        active: bool = True,
        searchable: bool = True,
        provider: str = "OPENAI",
        model_version: str | None = None,
    ) -> ModelRow:
        with self._store.transaction():
            item = ModelRow(
                self._id("model", model_id),
                name,
                dimension,
                active,
                searchable,
                provider,
                model_version or name,
            )
            self._store.insert_model(item)
            return item

    def ensure_embedding_model(
        self,
        *,
        provider: str,
        model_name: str,
        model_version: str,
        dimension: int,
    ) -> ModelRow:
        """Insert only an empty ledger; otherwise require one exact active model."""

        expected = (provider, model_name, model_version, dimension)
        with self._store.transaction():
            models = tuple(self._store.list_models())
            if not models:
                item = ModelRow(
                    self._id("model"),
                    model_name,
                    dimension,
                    True,
                    True,
                    provider,
                    model_version,
                )
                self._store.insert_model(item)
                return item
            active = tuple(item for item in models if item.active)
            if len(active) != 1 or (
                active[0].provider,
                active[0].name,
                active[0].model_version,
                active[0].dimension,
            ) != expected or not active[0].searchable:
                raise RuntimeError(
                    "embedding model registry must contain exactly one matching active model"
                )
            return active[0]

    def put_chunks(
        self, version_id: int, chunks: Iterable[object]
    ) -> tuple[ChunkRecord, ...]:
        converted = self._convert_chunks(version_id, chunks)
        with self._store.transaction():
            version = self._store.lock_version(version_id)
            if version is None:
                raise KeyError(version_id)
            self._store.save_chunks(version_id, converted)
            version.status = "CHUNKED"
            self._store.save_version(version)
        return converted

    def put_vectors(
        self,
        version_id: int,
        vectors: Iterable[Sequence[float]],
        *,
        model_id: int | None = None,
        status: str = "ACTIVE",
    ) -> tuple[VectorRow, ...]:
        with self._store.transaction():
            version = self._store.lock_version(version_id)
            if version is None:
                raise KeyError(version_id)
            model = self._active_model() if model_id is None else self._store.get_model(model_id)
            if model is None:
                raise KeyError(model_id)
            rows = tuple(
                VectorRow(
                    self._id("vector"),
                    version_id,
                    index,
                    model.id,
                    tuple(float(value) for value in vector),
                    status,
                )
                for index, vector in enumerate(vectors)
            )
            self._store.insert_vectors(rows)
            version.status = "EMBEDDING"
            self._store.save_version(version)
            return rows
