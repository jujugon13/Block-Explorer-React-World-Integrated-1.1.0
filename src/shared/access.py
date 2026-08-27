"""Small cross-feature contracts for resource authorization."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, MutableMapping
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeAlias

from .http import Principal

Identifier: TypeAlias = int | str


@dataclass(slots=True)
class CollectionRecord:
    id: int
    name: str
    owner_user_id: int
    parent_id: int | None = None
    visibility: str = "PRIVATE"
    status: str = "ACTIVE"

    @property
    def owner(self) -> int:
        return self.owner_user_id

    @property
    def parent(self) -> int | None:
        return self.parent_id


@dataclass(frozen=True, slots=True)
class DirectPermissionRecord:
    permission_id: int
    resource_kind: str
    resource_id: Identifier
    permission_kind: str
    target_type: str
    user_id: int | None = None
    department_id: int | None = None
    role_code: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CachedPermissionGrant:
    permission_id: int
    permission_kind: str
    expires_at: datetime | None


class PermissionLedgerStore(Protocol):
    def transaction(self) -> AbstractContextManager[object]: ...

    def lock_resource(
        self, resource_kind: str, resource_id: Identifier
    ) -> None: ...

    def lock_permission(self, permission_id: int) -> None: ...

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
    ) -> DirectPermissionRecord: ...

    def permission(self, permission_id: int) -> DirectPermissionRecord | None: ...

    def all_permissions(self) -> tuple[DirectPermissionRecord, ...]: ...

    def permissions_for_resource(
        self, resource_kind: str, resource_id: Identifier
    ) -> tuple[DirectPermissionRecord, ...]: ...

    def permissions_for_document(
        self, document_id: Identifier, collection_ids: Iterable[Identifier]
    ) -> tuple[DirectPermissionRecord, ...]: ...

    def delete_permission(self, permission_id: int) -> None: ...

    def cached_grants(
        self, user_id: int, document_id: Identifier
    ) -> tuple[CachedPermissionGrant, ...] | None: ...

    def put_cached_grant(
        self,
        user_id: int,
        document_id: Identifier,
        grant: CachedPermissionGrant,
    ) -> None: ...

    def replace_cached_grants(
        self,
        user_id: int,
        document_id: Identifier,
        grants: Iterable[CachedPermissionGrant],
    ) -> None: ...

    def invalidate_permission_cache(self, permission_id: int) -> None: ...

    def invalidate_document_cache(self, document_id: Identifier) -> None: ...

    def has_cached_grants(self) -> bool: ...


class PermissionMapView(MutableMapping[int, DirectPermissionRecord]):
    """Compatibility mapping over a permission store."""

    def __init__(self, store: PermissionLedgerStore) -> None:
        self.store = store

    def __getitem__(self, permission_id: int) -> DirectPermissionRecord:
        permission = self.store.permission(permission_id)
        if permission is None:
            raise KeyError(permission_id)
        return permission

    def __setitem__(
        self, permission_id: int, permission: DirectPermissionRecord
    ) -> None:
        del permission_id, permission
        raise TypeError("permission rows must be created through the ledger store")

    def __delitem__(self, permission_id: int) -> None:
        if self.store.permission(permission_id) is None:
            raise KeyError(permission_id)
        self.store.lock_permission(permission_id)
        self.store.delete_permission(permission_id)

    def __iter__(self):
        return (row.permission_id for row in self.store.all_permissions())

    def __len__(self) -> int:
        return len(self.store.all_permissions())


class CollectionLedgerStore(Protocol):
    def transaction(self) -> AbstractContextManager[object]: ...

    # The caller owns lock order; adapters must consume IDs in the supplied order.
    def lock_collections(self, collection_ids: Iterable[int]) -> None: ...

    def create_collection(
        self,
        name: str,
        owner_user_id: int,
        parent_id: int | None,
        visibility: str,
    ) -> CollectionRecord: ...

    def collection(self, collection_id: Identifier) -> CollectionRecord | None: ...

    def collections(self) -> tuple[CollectionRecord, ...]: ...

    def has_mapping(self, collection_id: int, document_id: Identifier) -> bool: ...

    def add_mapping(self, collection_id: int, document_id: Identifier) -> None: ...

    def remove_mapping(self, collection_id: int, document_id: Identifier) -> None: ...

    def remove_mappings(self, collection_ids: Iterable[int]) -> None: ...

    def set_visibility(self, collection_id: int, visibility: str) -> None: ...

    def set_status(self, collection_id: int, status: str) -> None: ...

    def collection_ids_for_document(
        self, document_id: Identifier
    ) -> frozenset[Identifier]: ...

    def document_ids_in_collection(
        self, collection_id: Identifier
    ) -> frozenset[Identifier]: ...

    def document_ids_in_collections(
        self, collection_ids: Iterable[int]
    ) -> frozenset[Identifier]: ...


class InMemoryPermissionLedgerStore:
    """The original process-local permission state behind the store seam."""

    def __init__(self) -> None:
        self.permissions: dict[int, DirectPermissionRecord] = {}
        self.user_cache: dict[
            tuple[int, Identifier], dict[int, CachedPermissionGrant]
        ] = {}
        self.next_id = 1

    @contextmanager
    def transaction(self) -> Iterator[object]:
        permissions_before = dict(self.permissions)
        cache_before = deepcopy(self.user_cache)
        next_id_before = self.next_id
        try:
            yield self
        except Exception:
            self.permissions.clear()
            self.permissions.update(permissions_before)
            self.user_cache.clear()
            self.user_cache.update(cache_before)
            self.next_id = next_id_before
            raise

    def lock_resource(
        self, resource_kind: str, resource_id: Identifier
    ) -> None:
        del resource_kind, resource_id

    def lock_permission(self, permission_id: int) -> None:
        del permission_id

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
        permission = DirectPermissionRecord(
            self.next_id,
            resource_kind,
            resource_id,
            permission_kind,
            target_type,
            user_id,
            department_id,
            role_code,
            expires_at,
        )
        self.next_id += 1
        self.permissions[permission.permission_id] = permission
        return permission

    def permission(self, permission_id: int) -> DirectPermissionRecord | None:
        return self.permissions.get(permission_id)

    def all_permissions(self) -> tuple[DirectPermissionRecord, ...]:
        return tuple(self.permissions.values())

    def permissions_for_resource(
        self, resource_kind: str, resource_id: Identifier
    ) -> tuple[DirectPermissionRecord, ...]:
        return tuple(
            permission
            for permission in self.permissions.values()
            if permission.resource_kind == resource_kind
            and permission.resource_id == resource_id
        )

    def permissions_for_document(
        self, document_id: Identifier, collection_ids: Iterable[Identifier]
    ) -> tuple[DirectPermissionRecord, ...]:
        identifiers = frozenset(collection_ids)
        return tuple(
            permission
            for permission in self.permissions.values()
            if (
                permission.resource_kind == "DOCUMENT"
                and permission.resource_id == document_id
            )
            or (
                permission.resource_kind == "COLLECTION"
                and permission.resource_id in identifiers
            )
        )

    def delete_permission(self, permission_id: int) -> None:
        self.permissions.pop(permission_id, None)

    def cached_grants(
        self, user_id: int, document_id: Identifier
    ) -> tuple[CachedPermissionGrant, ...] | None:
        grants = self.user_cache.get((user_id, document_id))
        return None if grants is None else tuple(grants.values())

    def put_cached_grant(
        self,
        user_id: int,
        document_id: Identifier,
        grant: CachedPermissionGrant,
    ) -> None:
        self.user_cache.setdefault((user_id, document_id), {})[
            grant.permission_id
        ] = grant

    def replace_cached_grants(
        self,
        user_id: int,
        document_id: Identifier,
        grants: Iterable[CachedPermissionGrant],
    ) -> None:
        key = (user_id, document_id)
        materialized = tuple(grants)
        self.user_cache.pop(key, None)
        if materialized:
            self.user_cache[key] = {
                grant.permission_id: grant for grant in materialized
            }

    def invalidate_permission_cache(self, permission_id: int) -> None:
        empty: list[tuple[int, Identifier]] = []
        for key, grants in self.user_cache.items():
            grants.pop(permission_id, None)
            if not grants:
                empty.append(key)
        for key in empty:
            self.user_cache.pop(key, None)

    def invalidate_document_cache(self, document_id: Identifier) -> None:
        for key in [key for key in self.user_cache if key[1] == document_id]:
            self.user_cache.pop(key, None)

    def has_cached_grants(self) -> bool:
        return bool(self.user_cache)


class InMemoryCollectionLedgerStore:
    """The original process-local collection state behind the store seam."""

    def __init__(self) -> None:
        self.rows: dict[int, CollectionRecord] = {}
        self.mappings: set[tuple[int, Identifier]] = set()
        self.next_id = 1

    @contextmanager
    def transaction(self) -> Iterator[object]:
        rows_before = dict(self.rows)
        values_before = {
            item_id: (
                item.name,
                item.owner_user_id,
                item.parent_id,
                item.visibility,
                item.status,
            )
            for item_id, item in self.rows.items()
        }
        mappings_before = set(self.mappings)
        next_id_before = self.next_id
        try:
            yield self
        except Exception:
            self.rows.clear()
            self.rows.update(rows_before)
            for item_id, values in values_before.items():
                item = self.rows[item_id]
                (
                    item.name,
                    item.owner_user_id,
                    item.parent_id,
                    item.visibility,
                    item.status,
                ) = values
            self.mappings.clear()
            self.mappings.update(mappings_before)
            self.next_id = next_id_before
            raise

    def lock_collections(self, collection_ids: Iterable[int]) -> None:
        del collection_ids

    def create_collection(
        self,
        name: str,
        owner_user_id: int,
        parent_id: int | None,
        visibility: str,
    ) -> CollectionRecord:
        collection = CollectionRecord(
            self.next_id, name, owner_user_id, parent_id, visibility
        )
        self.next_id += 1
        self.rows[collection.id] = collection
        return collection

    def collection(self, collection_id: Identifier) -> CollectionRecord | None:
        return self.rows.get(collection_id)  # type: ignore[arg-type]

    def collections(self) -> tuple[CollectionRecord, ...]:
        return tuple(self.rows.values())

    def has_mapping(self, collection_id: int, document_id: Identifier) -> bool:
        return (collection_id, document_id) in self.mappings

    def add_mapping(self, collection_id: int, document_id: Identifier) -> None:
        self.mappings.add((collection_id, document_id))

    def remove_mapping(self, collection_id: int, document_id: Identifier) -> None:
        self.mappings.remove((collection_id, document_id))

    def remove_mappings(self, collection_ids: Iterable[int]) -> None:
        identifiers = frozenset(collection_ids)
        retained = {
            mapping for mapping in self.mappings if mapping[0] not in identifiers
        }
        self.mappings.clear()
        self.mappings.update(retained)

    def set_visibility(self, collection_id: int, visibility: str) -> None:
        self.rows[collection_id].visibility = visibility

    def set_status(self, collection_id: int, status: str) -> None:
        self.rows[collection_id].status = status

    def collection_ids_for_document(
        self, document_id: Identifier
    ) -> frozenset[Identifier]:
        return frozenset(
            collection_id
            for collection_id, mapped_document_id in self.mappings
            if mapped_document_id == document_id
        )

    def document_ids_in_collection(
        self, collection_id: Identifier
    ) -> frozenset[Identifier]:
        return frozenset(
            document_id
            for mapped_collection_id, document_id in self.mappings
            if mapped_collection_id == collection_id
        )

    def document_ids_in_collections(
        self, collection_ids: Iterable[int]
    ) -> frozenset[Identifier]:
        identifiers = frozenset(collection_ids)
        return frozenset(
            document_id
            for collection_id, document_id in self.mappings
            if collection_id in identifiers
        )


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    resource_id: Identifier
    owner_user_id: int
    visibility: str
    status: str


class DocumentAccessCatalog(Protocol):
    def document_access(
        self, document_id: Identifier, *, include_deleted: bool = False
    ) -> ResourceAccess | None: ...

    def document_ids(self) -> frozenset[Identifier]: ...


class CollectionAccessCatalog(Protocol):
    def collection_access(
        self, collection_id: Identifier, *, include_deleted: bool = False
    ) -> ResourceAccess | None: ...

    def collection_ids_for_document(self, document_id: Identifier) -> frozenset[Identifier]: ...

    def document_ids_in_collection(self, collection_id: Identifier) -> frozenset[Identifier]: ...


class PermissionReader(Protocol):
    def readable_document_ids(
        self, principal: Principal, candidate_ids: Iterable[Identifier]
    ) -> frozenset[Identifier]: ...

    def live_readable_document_ids(
        self, principal: Principal, candidate_ids: Iterable[Identifier]
    ) -> frozenset[Identifier]: ...

    def can_read_document(self, principal: Principal, document_id: Identifier) -> bool: ...


class CollectionPermissionUnitOfWork(Protocol):
    """Keep collection deletion, permission cleanup, and outbox writes atomic."""

    def execute_collection_delete(
        self, plan: AbstractContextManager["CollectionDeletePlan"]
    ) -> None: ...


class CollectionDeletePlan(Protocol):
    collection_ids: Iterable[Identifier]
    document_ids: Iterable[Identifier]

    def apply(self) -> None: ...
