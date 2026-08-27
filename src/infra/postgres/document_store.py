"""PostgreSQL implementation of the shared document-ledger port."""

from __future__ import annotations

from typing import Any

from src.shared import StorageLocation
from src.shared.document_ledger import DocumentLedgerStore

from .document_rows import (
    DocumentRow,
    DocumentVersionRow,
    FileObjectRow,
    IndexJobRow,
    StoredChunkRow,
)
from .transaction import PostgresTransactionManager


_FILE_COLUMNS = """
    file_object_id, content_sha256, file_size, original_filename,
    content_type, document_type, storage_provider, storage_namespace, storage_key
"""
_DOCUMENT_COLUMNS = """
    document_id, title, description, document_type, source_type, status,
    visibility, owner_user_id, owner_name, current_version_id,
    latest_version_id, created_at, updated_at, deleted_at
"""
_VERSION_COLUMNS = """
    document_version_id, document_id, version_no, file_object_id,
    title_snapshot, status, created_at, indexed_at
"""
_JOB_COLUMNS = "job_id, document_version_id, created_at, status"
_CHUNK_COLUMNS = """
    chunk_index, start_offset, end_offset, content, content_sha256,
    token_estimate, page_number, section_title
"""
_IDENTITIES = {
    "file": ("file_objects", "file_object_id"),
    "document": ("documents", "document_id"),
    "version": ("document_versions", "document_version_id"),
    "job": ("indexing_jobs", "job_id"),
}


def _close(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


def _run(connection: Any, sql: str, parameters: tuple[object, ...] = ()) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
    finally:
        _close(cursor)


def _fetchone(
    connection: Any, sql: str, parameters: tuple[object, ...] = ()
) -> object | None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return cursor.fetchone()
    finally:
        _close(cursor)


def _fetchall(
    connection: Any, sql: str, parameters: tuple[object, ...] = ()
) -> list[object]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return list(cursor.fetchall())
    finally:
        _close(cursor)


def _values(row: object) -> tuple[object, ...]:
    return tuple(row)  # type: ignore[arg-type]


def _file(row: object) -> FileObjectRow:
    value = _values(row)
    size = int(value[2])
    return FileObjectRow(
        int(value[0]),
        str(value[1]),
        size,
        str(value[3]),
        str(value[4]),
        str(value[5]),
        StorageLocation(str(value[6]).lower(), str(value[7]), str(value[8]), size),
    )


def _document(row: object) -> DocumentRow:
    value = _values(row)
    return DocumentRow(
        int(value[0]),
        str(value[1]),
        None if value[2] is None else str(value[2]),
        str(value[3]),
        str(value[4]),
        str(value[5]),
        str(value[6]),
        int(value[7]),
        str(value[8]),
        None if value[9] is None else int(value[9]),
        int(value[10]),
        value[11],  # type: ignore[arg-type]
        value[12],  # type: ignore[arg-type]
        value[13],  # type: ignore[arg-type]
    )


def _version(row: object) -> DocumentVersionRow:
    value = _values(row)
    return DocumentVersionRow(
        int(value[0]),
        int(value[1]),
        int(value[2]),
        int(value[3]),
        str(value[4]),
        str(value[5]),
        value[6],  # type: ignore[arg-type]
        value[7],  # type: ignore[arg-type]
    )


def _job(row: object) -> IndexJobRow:
    value = _values(row)
    return IndexJobRow(
        int(value[0]), int(value[1]), value[2], str(value[3])  # type: ignore[arg-type]
    )


def _chunk(row: object) -> StoredChunkRow:
    value = _values(row)
    return StoredChunkRow(
        int(value[0]),
        int(value[1]),
        int(value[2]),
        str(value[3]),
        str(value[4]),
        int(value[5]),
        None if value[6] is None else int(value[6]),
        None if value[7] is None else str(value[7]),
    )


class PostgresDocumentStore(DocumentLedgerStore):
    """Persist document state while leaving every domain decision to its caller."""

    def __init__(self, manager: PostgresTransactionManager) -> None:
        self._manager = manager

    def read(self):
        return self._manager.transaction()

    def transaction(self):
        return self._manager.transaction()

    def next_id(self, kind: str) -> int:
        if kind == "event":
            return 0
        table, column = _IDENTITIES[kind]
        sql = "SELECT nextval(pg_get_serial_sequence(%s, %s))"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (table, column))
        if row is None:
            raise RuntimeError(f"identity sequence missing for {kind}")
        return int(_values(row)[0])

    def _lock(self, table: str, column: str, identifier: int) -> None:
        sql = f"SELECT {column} FROM {table} WHERE {column} = %s FOR UPDATE"
        with self._manager.operation() as connection:
            _fetchone(connection, sql, (identifier,))

    def lock_job(self, job_id: int) -> None:
        self._lock("indexing_jobs", "job_id", job_id)

    def lock_version(self, version_id: int) -> None:
        self._lock("document_versions", "document_version_id", version_id)

    def lock_document(self, document_id: int) -> None:
        self._lock("documents", "document_id", document_id)

    def find_file(self, digest: str, size: int) -> FileObjectRow | None:
        sql = f"SELECT {_FILE_COLUMNS} FROM file_objects WHERE content_sha256 = %s AND file_size = %s"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (digest, size))
        return None if row is None else _file(row)

    def file(self, file_id: int) -> FileObjectRow | None:
        sql = f"SELECT {_FILE_COLUMNS} FROM file_objects WHERE file_object_id = %s"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (file_id,))
        return None if row is None else _file(row)

    def is_referenced(self, location: StorageLocation) -> bool:
        sql = """
            SELECT 1
            FROM file_objects
            WHERE storage_provider = %s
              AND storage_namespace = %s
              AND storage_key = %s
            LIMIT 1
        """
        parameters = (
            location.provider.upper(),
            location.namespace,
            location.key,
        )
        with self._manager.operation() as connection:
            return _fetchone(connection, sql, parameters) is not None

    def insert_file_if_absent(self, row: Any) -> bool:
        sql = """
            INSERT INTO file_objects (
                file_object_id, content_sha256, file_size, original_filename,
                content_type, document_type, storage_provider,
                storage_namespace, storage_key
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (content_sha256, file_size) DO NOTHING
            RETURNING file_object_id
        """
        parameters = (
            row.id, row.digest, row.size, row.filename, row.content_type,
            row.document_type, row.location.provider.upper(),
            row.location.namespace, row.location.key,
        )
        with self._manager.operation() as connection:
            inserted = _fetchone(connection, sql, parameters)
        return inserted is not None

    def document(self, document_id: int) -> DocumentRow | None:
        sql = f"SELECT {_DOCUMENT_COLUMNS} FROM documents WHERE document_id = %s"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (document_id,))
        return None if row is None else _document(row)

    def documents(self) -> tuple[DocumentRow, ...]:
        sql = f"SELECT {_DOCUMENT_COLUMNS} FROM documents ORDER BY document_id"
        with self._manager.operation() as connection:
            rows = _fetchall(connection, sql)
        return tuple(_document(row) for row in rows)

    def insert_document(self, row: Any) -> None:
        sql = """
            INSERT INTO documents (
                document_id, title, description, document_type, source_type,
                status, visibility, owner_user_id, owner_name,
                current_version_id, latest_version_id, created_at, updated_at,
                deleted_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        parameters = (
            row.id, row.title, row.description, row.document_type,
            row.source_type, row.status, row.visibility, row.owner_user_id,
            row.owner_name, row.current_version_id, row.latest_version_id,
            row.created_at, row.updated_at, row.deleted_at,
        )
        with self._manager.operation() as connection:
            _run(connection, sql, parameters)

    def save_document(self, row: Any) -> None:
        sql = """
            UPDATE documents SET
                title = %s, description = %s, document_type = %s,
                source_type = %s, status = %s, visibility = %s,
                owner_user_id = %s, owner_name = %s, current_version_id = %s,
                latest_version_id = %s, created_at = %s, updated_at = %s,
                deleted_at = %s
            WHERE document_id = %s
        """
        parameters = (
            row.title, row.description, row.document_type, row.source_type,
            row.status, row.visibility, row.owner_user_id, row.owner_name,
            row.current_version_id, row.latest_version_id, row.created_at,
            row.updated_at, row.deleted_at, row.id,
        )
        with self._manager.operation() as connection:
            _run(connection, sql, parameters)

    def version(self, version_id: int) -> DocumentVersionRow | None:
        sql = f"SELECT {_VERSION_COLUMNS} FROM document_versions WHERE document_version_id = %s"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (version_id,))
        return None if row is None else _version(row)

    def versions(self, document_id: int) -> tuple[DocumentVersionRow, ...]:
        sql = f"SELECT {_VERSION_COLUMNS} FROM document_versions WHERE document_id = %s ORDER BY version_no"
        with self._manager.operation() as connection:
            rows = _fetchall(connection, sql, (document_id,))
        return tuple(_version(row) for row in rows)

    def insert_version(self, row: Any) -> None:
        sql = """
            INSERT INTO document_versions (
                document_version_id, document_id, version_no, file_object_id,
                title_snapshot, status, created_at, indexed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        parameters = (
            row.id, row.document_id, row.version_no, row.file_object_id,
            row.title_snapshot, row.status, row.created_at, row.indexed_at,
        )
        with self._manager.operation() as connection:
            _run(connection, sql, parameters)

    def save_version(self, row: Any) -> None:
        sql = """
            UPDATE document_versions SET
                document_id = %s, version_no = %s, file_object_id = %s,
                title_snapshot = %s, status = %s, created_at = %s,
                indexed_at = %s
            WHERE document_version_id = %s
        """
        parameters = (
            row.document_id, row.version_no, row.file_object_id,
            row.title_snapshot, row.status, row.created_at, row.indexed_at,
            row.id,
        )
        with self._manager.operation() as connection:
            _run(connection, sql, parameters)

    def job(self, job_id: int) -> IndexJobRow | None:
        sql = f"SELECT {_JOB_COLUMNS} FROM indexing_jobs WHERE job_id = %s"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (job_id,))
        return None if row is None else _job(row)

    def job_for_version(self, version_id: int) -> IndexJobRow | None:
        sql = f"SELECT {_JOB_COLUMNS} FROM indexing_jobs WHERE document_version_id = %s"
        with self._manager.operation() as connection:
            row = _fetchone(connection, sql, (version_id,))
        return None if row is None else _job(row)

    def insert_job(self, row: Any) -> None:
        sql = """
            INSERT INTO indexing_jobs (
                job_id, document_version_id, status, created_at
            ) VALUES (%s, %s, %s, %s)
        """
        with self._manager.operation() as connection:
            _run(connection, sql, (
                row.id, row.document_version_id, row.status, row.created_at,
            ))

    def save_job(self, row: Any) -> None:
        with self._manager.operation() as connection:
            _run(
                connection,
                "UPDATE indexing_jobs SET status = %s WHERE job_id = %s",
                (row.status, row.id),
            )

    def _chunks(self, connection: Any, version_id: int) -> tuple[StoredChunkRow, ...]:
        sql = f"SELECT {_CHUNK_COLUMNS} FROM document_chunks WHERE document_version_id = %s ORDER BY chunk_index"
        return tuple(_chunk(row) for row in _fetchall(connection, sql, (version_id,)))

    def chunks(self, version_id: int) -> tuple[StoredChunkRow, ...] | None:
        with self._manager.operation() as connection:
            rows = self._chunks(connection, version_id)
        return rows or None

    @staticmethod
    def _insert_chunks(connection: Any, version_id: int, rows: tuple[Any, ...]) -> int:
        sql = """
            INSERT INTO document_chunks (
                document_version_id, chunk_index, start_offset, end_offset,
                content, content_sha256, token_estimate, page_number,
                section_title
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_version_id, chunk_index) DO NOTHING
            RETURNING chunk_index
        """
        inserted = 0
        cursor = connection.cursor()
        try:
            for row in rows:
                cursor.execute(sql, (
                    version_id, row.index, row.start, row.end, row.text,
                    row.text_sha256, row.token_estimate, row.page_number,
                    row.section_title,
                ))
                inserted += cursor.fetchone() is not None
        finally:
            _close(cursor)
        return inserted

    def insert_chunks_if_absent(
        self, version_id: int, rows: tuple[Any, ...]
    ) -> tuple[bool, tuple[StoredChunkRow, ...]]:
        with self._manager.operation() as connection:
            existing = self._chunks(connection, version_id)
            if existing:
                return False, existing
            inserted = self._insert_chunks(connection, version_id, rows)
            if inserted != len(rows):
                return False, self._chunks(connection, version_id)
        return True, ()

    def replace_chunks(self, version_id: int, rows: tuple[Any, ...]) -> None:
        with self._manager.operation() as connection:
            _run(
                connection,
                "DELETE FROM document_chunks WHERE document_version_id = %s",
                (version_id,),
            )
            self._insert_chunks(connection, version_id, rows)

    def append_event(self, row: Any) -> None:
        # SyncOutbox is the sole durable event source; this legacy shadow is omitted.
        del row

    def has_documents(self) -> bool:
        with self._manager.operation() as connection:
            row = _fetchone(connection, "SELECT EXISTS (SELECT 1 FROM documents)")
        return bool(row and _values(row)[0])

    def compatibility_state(self) -> Any:
        raise RuntimeError("PostgreSQL document storage has no mutable state alias")

    def compatibility_next_ids(self) -> dict[str, int]:
        raise RuntimeError("PostgreSQL document storage has no mutable identity alias")
