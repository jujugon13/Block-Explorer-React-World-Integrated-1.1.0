"""PostgreSQL persistence for collection rows and document mappings."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.shared.access import CollectionRecord, Identifier

from .transaction import PostgresTransactionManager


_COLLECTION_COLUMNS = (
    "collection_id, name, owner_user_id, parent_id, visibility, status"
)


def _close(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


def _execute(connection: Any, sql: str, parameters: object = None) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql) if parameters is None else cursor.execute(sql, parameters)
    finally:
        _close(cursor)


def _fetchone(connection: Any, sql: str, parameters: object = None):
    cursor = connection.cursor()
    try:
        cursor.execute(sql) if parameters is None else cursor.execute(sql, parameters)
        return cursor.fetchone()
    finally:
        _close(cursor)


def _fetchall(connection: Any, sql: str, parameters: object = None):
    cursor = connection.cursor()
    try:
        cursor.execute(sql) if parameters is None else cursor.execute(sql, parameters)
        return cursor.fetchall()
    finally:
        _close(cursor)


def _record(row: object) -> CollectionRecord:
    if row is None:
        raise RuntimeError("collection insert returned no row")
    values = tuple(row)  # type: ignore[arg-type]
    return CollectionRecord(
        int(values[0]),
        str(values[1]),
        int(values[2]),
        None if values[3] is None else int(values[3]),
        str(values[4]),
        str(values[5]),
    )


class PostgresCollectionStore:
    def __init__(self, manager: PostgresTransactionManager) -> None:
        self.manager = manager

    def transaction(self):
        return self.manager.transaction()

    def lock_collections(self, collection_ids: Iterable[int]) -> None:
        with self.manager.operation() as connection:
            for collection_id in collection_ids:
                _fetchone(
                    connection,
                    "SELECT collection_id FROM collections "
                    "WHERE collection_id = %s FOR UPDATE",
                    (collection_id,),
                )

    def create_collection(
        self,
        name: str,
        owner_user_id: int,
        parent_id: int | None,
        visibility: str,
    ) -> CollectionRecord:
        with self.manager.operation() as connection:
            row = _fetchone(
                connection,
                "INSERT INTO collections "
                "(name, owner_user_id, parent_id, visibility, status) "
                "VALUES (%s, %s, %s, %s, 'ACTIVE') "
                f"RETURNING {_COLLECTION_COLUMNS}",
                (name, owner_user_id, parent_id, visibility),
            )
        return _record(row)

    def collection(self, collection_id: Identifier) -> CollectionRecord | None:
        with self.manager.operation() as connection:
            row = _fetchone(
                connection,
                f"SELECT {_COLLECTION_COLUMNS} FROM collections "
                "WHERE collection_id = %s",
                (collection_id,),
            )
        return None if row is None else _record(row)

    def collections(self) -> tuple[CollectionRecord, ...]:
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                f"SELECT {_COLLECTION_COLUMNS} FROM collections "
                "ORDER BY collection_id",
            )
        return tuple(_record(row) for row in rows)

    def has_mapping(self, collection_id: int, document_id: Identifier) -> bool:
        with self.manager.operation() as connection:
            row = _fetchone(
                connection,
                "SELECT EXISTS (SELECT 1 FROM collection_documents "
                "WHERE collection_id = %s AND document_id = %s)",
                (collection_id, document_id),
            )
        return bool(row and row[0])

    def add_mapping(self, collection_id: int, document_id: Identifier) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "INSERT INTO collection_documents (collection_id, document_id) "
                "VALUES (%s, %s)",
                (collection_id, document_id),
            )

    def remove_mapping(self, collection_id: int, document_id: Identifier) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "DELETE FROM collection_documents "
                "WHERE collection_id = %s AND document_id = %s",
                (collection_id, document_id),
            )

    def remove_mappings(self, collection_ids: Iterable[int]) -> None:
        identifiers = tuple(collection_ids)
        if not identifiers:
            return
        with self.manager.operation() as connection:
            _execute(
                connection,
                "DELETE FROM collection_documents WHERE collection_id = ANY(%s)",
                (list(identifiers),),
            )

    def set_visibility(self, collection_id: int, visibility: str) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "UPDATE collections SET visibility = %s WHERE collection_id = %s",
                (visibility, collection_id),
            )

    def set_status(self, collection_id: int, status: str) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "UPDATE collections SET status = %s WHERE collection_id = %s",
                (status, collection_id),
            )

    def collection_ids_for_document(
        self, document_id: Identifier
    ) -> frozenset[Identifier]:
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                "SELECT collection_id FROM collection_documents "
                "WHERE document_id = %s ORDER BY collection_id",
                (document_id,),
            )
        return frozenset(int(row[0]) for row in rows)

    def document_ids_in_collection(
        self, collection_id: Identifier
    ) -> frozenset[Identifier]:
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                "SELECT document_id FROM collection_documents "
                "WHERE collection_id = %s ORDER BY document_id",
                (collection_id,),
            )
        return frozenset(int(row[0]) for row in rows)

    def document_ids_in_collections(
        self, collection_ids: Iterable[int]
    ) -> frozenset[Identifier]:
        identifiers = tuple(collection_ids)
        if not identifiers:
            return frozenset()
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                "SELECT DISTINCT document_id FROM collection_documents "
                "WHERE collection_id = ANY(%s) ORDER BY document_id",
                (list(identifiers),),
            )
        return frozenset(int(row[0]) for row in rows)
