"""PostgreSQL persistence for direct permissions and user projections."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from src.shared.access import (
    CachedPermissionGrant,
    DirectPermissionRecord,
    Identifier,
)

from .collection_store import _execute, _fetchall, _fetchone
from .transaction import PostgresTransactionManager


_PERMISSION_COLUMNS = (
    "permission_id, permission_kind, target_type, document_id, collection_id, "
    "user_id, department_id, role_code, expires_at"
)
_RESOURCES = {
    "DOCUMENT": ("documents", "document_id"),
    "COLLECTION": ("collections", "collection_id"),
}


def _resource(resource_kind: str) -> tuple[str, str]:
    try:
        return _RESOURCES[resource_kind]
    except KeyError:
        raise ValueError("unsupported permission resource kind") from None


def _record(row: object) -> DirectPermissionRecord:
    if row is None:
        raise RuntimeError("permission insert returned no row")
    values = tuple(row)  # type: ignore[arg-type]
    document_id, collection_id = values[3], values[4]
    resource_kind = "DOCUMENT" if document_id is not None else "COLLECTION"
    resource_id = document_id if document_id is not None else collection_id
    if resource_id is None:
        raise RuntimeError("permission row has no resource")
    return DirectPermissionRecord(
        int(values[0]),
        resource_kind,
        int(resource_id),
        str(values[1]),
        str(values[2]),
        None if values[5] is None else int(values[5]),
        None if values[6] is None else int(values[6]),
        None if values[7] is None else str(values[7]),
        values[8],
    )


def _put_cache(
    connection: Any,
    user_id: int,
    document_id: Identifier,
    permission_id: int,
) -> None:
    _execute(
        connection,
        "INSERT INTO document_permission_cache (permission_id, document_id) "
        "SELECT permission_id, %s FROM direct_permissions "
        "WHERE permission_id = %s AND target_type = 'USER' AND user_id = %s "
        "ON CONFLICT (permission_id, document_id) DO NOTHING",
        (document_id, permission_id, user_id),
    )


class PostgresPermissionStore:
    def __init__(self, manager: PostgresTransactionManager) -> None:
        self.manager = manager

    def transaction(self):
        return self.manager.transaction()

    def lock_resource(
        self, resource_kind: str, resource_id: Identifier
    ) -> None:
        table, column = _resource(resource_kind)
        with self.manager.operation() as connection:
            _fetchone(
                connection,
                f"SELECT {column} FROM {table} WHERE {column} = %s FOR UPDATE",
                (resource_id,),
            )

    def lock_permission(self, permission_id: int) -> None:
        with self.manager.operation() as connection:
            _fetchone(
                connection,
                "SELECT permission_id FROM direct_permissions "
                "WHERE permission_id = %s FOR UPDATE",
                (permission_id,),
            )

    def create_permission(
        self,
        resource_kind: str,
        resource_id: Identifier,
        permission_kind: str,
        target_type: str,
        user_id: int | None,
        department_id: int | None,
        role_code: str | None,
        expires_at: datetime | None,
    ) -> DirectPermissionRecord:
        _table, resource_column = _resource(resource_kind)
        document_id = resource_id if resource_column == "document_id" else None
        collection_id = resource_id if resource_column == "collection_id" else None
        with self.manager.operation() as connection:
            row = _fetchone(
                connection,
                "INSERT INTO direct_permissions "
                "(permission_kind, target_type, document_id, collection_id, "
                "user_id, department_id, role_code, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_PERMISSION_COLUMNS}",
                (
                    permission_kind,
                    target_type,
                    document_id,
                    collection_id,
                    user_id,
                    department_id,
                    role_code,
                    expires_at,
                ),
            )
        return _record(row)

    def permission(self, permission_id: int) -> DirectPermissionRecord | None:
        with self.manager.operation() as connection:
            row = _fetchone(
                connection,
                f"SELECT {_PERMISSION_COLUMNS} FROM direct_permissions "
                "WHERE permission_id = %s",
                (permission_id,),
            )
        return None if row is None else _record(row)

    def all_permissions(self) -> tuple[DirectPermissionRecord, ...]:
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                f"SELECT {_PERMISSION_COLUMNS} FROM direct_permissions "
                "ORDER BY permission_id",
            )
        return tuple(_record(row) for row in rows)

    def permissions_for_resource(
        self, resource_kind: str, resource_id: Identifier
    ) -> tuple[DirectPermissionRecord, ...]:
        _table, column = _resource(resource_kind)
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                f"SELECT {_PERMISSION_COLUMNS} FROM direct_permissions "
                f"WHERE {column} = %s ORDER BY permission_id",
                (resource_id,),
            )
        return tuple(_record(row) for row in rows)

    def permissions_for_document(
        self, document_id: Identifier, collection_ids: Iterable[Identifier]
    ) -> tuple[DirectPermissionRecord, ...]:
        identifiers = tuple(collection_ids)
        parameters: object
        if identifiers:
            predicate = "document_id = %s OR collection_id = ANY(%s)"
            parameters = (document_id, list(identifiers))
        else:
            predicate = "document_id = %s"
            parameters = (document_id,)
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                f"SELECT {_PERMISSION_COLUMNS} FROM direct_permissions "
                f"WHERE {predicate} ORDER BY permission_id",
                parameters,
            )
        return tuple(_record(row) for row in rows)

    def delete_permission(self, permission_id: int) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "DELETE FROM direct_permissions WHERE permission_id = %s",
                (permission_id,),
            )

    def cached_grants(
        self, user_id: int, document_id: Identifier
    ) -> tuple[CachedPermissionGrant, ...] | None:
        with self.manager.operation() as connection:
            rows = _fetchall(
                connection,
                "SELECT p.permission_id, p.permission_kind, p.expires_at "
                "FROM document_permission_cache AS c "
                "JOIN direct_permissions AS p ON p.permission_id = c.permission_id "
                "WHERE p.target_type = 'USER' AND p.user_id = %s "
                "AND c.document_id = %s ORDER BY p.permission_id",
                (user_id, document_id),
            )
        if not rows:
            return None
        return tuple(
            CachedPermissionGrant(int(row[0]), str(row[1]), row[2]) for row in rows
        )

    def put_cached_grant(
        self,
        user_id: int,
        document_id: Identifier,
        grant: CachedPermissionGrant,
    ) -> None:
        with self.manager.operation() as connection:
            _put_cache(connection, user_id, document_id, grant.permission_id)

    def replace_cached_grants(
        self,
        user_id: int,
        document_id: Identifier,
        grants: Iterable[CachedPermissionGrant],
    ) -> None:
        materialized = tuple(grants)
        with self.manager.operation() as connection:
            _execute(
                connection,
                "DELETE FROM document_permission_cache AS c "
                "USING direct_permissions AS p "
                "WHERE p.permission_id = c.permission_id "
                "AND p.target_type = 'USER' AND p.user_id = %s "
                "AND c.document_id = %s",
                (user_id, document_id),
            )
            for grant in materialized:
                _put_cache(connection, user_id, document_id, grant.permission_id)

    def invalidate_permission_cache(self, permission_id: int) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "DELETE FROM document_permission_cache WHERE permission_id = %s",
                (permission_id,),
            )

    def invalidate_document_cache(self, document_id: Identifier) -> None:
        with self.manager.operation() as connection:
            _execute(
                connection,
                "DELETE FROM document_permission_cache WHERE document_id = %s",
                (document_id,),
            )

    def has_cached_grants(self) -> bool:
        with self.manager.operation() as connection:
            row = _fetchone(
                connection,
                "SELECT EXISTS (SELECT 1 FROM document_permission_cache)",
            )
        return bool(row and row[0])
