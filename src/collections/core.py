"""Collection behavior with in-process relational state."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock

from src.shared import (
    CollectionPermissionUnitOfWork,
    DocumentAccessCatalog,
    Identifier,
    PermissionReader,
    Principal,
    PublicError,
    ResourceAccess,
)
from src.shared.access import (
    CollectionLedgerStore,
    CollectionRecord,
    InMemoryCollectionLedgerStore,
)

CREATE_VISIBILITIES = frozenset({"PRIVATE", "COLLECTION", "DEPARTMENT", "PUBLIC"})
EDIT_VISIBILITIES = frozenset({"PRIVATE", "PUBLIC"})

AccessDecider = Callable[[Principal, "Collection", str], bool]
CacheInvalidator = Callable[[Identifier], None]
PermissionCleanup = Callable[[Iterable[Identifier]], None]


Collection = CollectionRecord


@dataclass(frozen=True, slots=True)
class _DeletePlan:
    collection_ids: tuple[int, ...]
    document_ids: frozenset[Identifier]
    apply: Callable[[], None]


class CollectionWorkspace:
    """CollectionAccessCatalog implementation and collection command surface."""

    def __init__(
        self,
        *,
        access_decider: AccessDecider | None = None,
        documents: DocumentAccessCatalog | None = None,
        permission_reader: PermissionReader | None = None,
        permission_cleanup: CollectionPermissionUnitOfWork | None = None,
        invalidate_document_cache: CacheInvalidator | None = None,
        cleanup_collection_permissions: PermissionCleanup | None = None,
        store: CollectionLedgerStore | None = None,
    ) -> None:
        self._access_decider = access_decider or self._default_access
        self._documents = documents
        self._permission_reader = permission_reader
        bound_cleanup = getattr(cleanup_collection_permissions, "__self__", None)
        self._permission_cleanup = permission_cleanup or (
            bound_cleanup
            if hasattr(bound_cleanup, "execute_collection_delete")
            else None
        )
        self._invalidate_document_cache = invalidate_document_cache
        self._cleanup_collection_permissions = cleanup_collection_permissions
        self._store = store if store is not None else InMemoryCollectionLedgerStore()
        self._lock = RLock()

    @property
    def _collections(self):
        """Compatibility view for the original in-memory state."""

        return self._store.rows  # type: ignore[attr-defined]

    @property
    def _mappings(self):
        """Compatibility view for the original in-memory state."""

        return self._store.mappings  # type: ignore[attr-defined]

    @property
    def _next_id(self):
        """Compatibility view for the original in-memory state."""

        return self._store.next_id  # type: ignore[attr-defined]

    def bind_permissions(self, service: object) -> None:
        """Complete the collection/permission cycle at the composition root."""

        required = (
            "readable_document_ids",
            "can_read_document",
            "execute_collection_delete",
            "invalidate_document_cache",
            "collection_decider",
        )
        if any(not callable(getattr(service, name, None)) for name in required):
            raise TypeError("permission service does not implement collection ports")
        service_documents = getattr(service, "documents", None)
        with self._lock:
            if self._permission_reader not in {None, service}:
                raise RuntimeError("collections are already bound to another permission reader")
            if self._permission_cleanup not in {None, service}:
                raise RuntimeError("collections are already bound to another permission unit of work")
            if self._documents is not None and service_documents is not None and self._documents is not service_documents:
                raise RuntimeError("collections and permissions use different document catalogs")
            self._permission_reader = service  # type: ignore[assignment]
            self._permission_cleanup = service  # type: ignore[assignment]
            if self._documents is None:
                self._documents = service_documents
            self._invalidate_document_cache = service.invalidate_document_cache
            if self._access_decider == self._default_access:
                self._access_decider = service.collection_decider

    @staticmethod
    def _default_access(principal: Principal, collection: Collection, action: str) -> bool:
        if principal.user_id == collection.owner_user_id:
            return True
        return action == "READ" and collection.visibility == "PUBLIC"

    @staticmethod
    def _fail(code: str) -> None:
        raise PublicError(code)

    def _active(self, collection_id: int) -> Collection:
        collection = self._store.collection(collection_id)
        if collection is None or collection.status == "DELETED":
            self._fail("COLLECTION-001")
        return collection

    def _require(self, principal: Principal, collection: Collection, action: str) -> None:
        if not self._access_decider(principal, collection, action):
            self._fail("ROLE-002")

    @staticmethod
    def _require_owner(principal: Principal, collection: Collection) -> None:
        if principal.user_id != collection.owner_user_id:
            raise PublicError("ROLE-002")

    def create(
        self,
        principal: Principal,
        name: str,
        *,
        parent_id: int | None = None,
        visibility: str | None = None,
    ) -> Collection:
        selected_visibility = "PRIVATE" if visibility is None else visibility
        if selected_visibility not in CREATE_VISIBILITIES:
            self._fail("COMMON-002")
        if principal.user_id is None:
            self._fail("ROLE-002")
        if parent_id is not None:
            with self._lock:
                parent = self._active(parent_id)
            self._require(principal, parent, "WRITE")
        with self._lock:
            with self._store.transaction():
                if parent_id is not None:
                    self._store.lock_collections((parent_id,))
                    self._active(parent_id)
                collection = self._store.create_collection(
                    name,
                    principal.user_id,
                    parent_id,
                    selected_visibility,
                )
                return collection

    def get(self, principal: Principal, collection_id: int) -> Collection:
        with self._lock:
            collection = self._active(collection_id)
        self._require(principal, collection, "READ")
        return collection

    def list(
        self,
        principal: Principal,
        *,
        keyword: str | None = None,
        page: int = 0,
        size: int = 20,
    ) -> dict[str, object]:
        if page < 0 or not 1 <= size <= 100:
            self._fail("COMMON-002")
        needle = keyword.casefold() if keyword else None
        with self._lock:
            candidates = [
                item
                for item in self._store.collections()
                if item.status != "DELETED"
                and (needle is None or needle in item.name.casefold())
            ]
        matched = [
            item for item in candidates if self._access_decider(principal, item, "READ")
        ]
        matched.sort(key=lambda item: item.id)
        total = len(matched)
        pages = math.ceil(total / size) if total else 0
        return {
            "content": matched[page * size : (page + 1) * size],
            "page": page,
            "size": size,
            "totalElements": total,
            "totalPages": pages,
            "first": page == 0,
            "last": page >= max(0, pages - 1),
        }

    def children(self, principal: Principal, collection_id: int) -> tuple[Collection, ...]:
        with self._lock:
            parent = self._active(collection_id)
        self._require(principal, parent, "READ")
        with self._lock:
            self._active(collection_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._store.collections()
                        if item.status != "DELETED" and item.parent_id == collection_id
                    ),
                    key=lambda item: item.id,
                )
            )

    def documents(self, principal: Principal, collection_id: int) -> tuple[Identifier, ...]:
        with self._lock:
            collection = self._active(collection_id)
        self._require(principal, collection, "READ")
        candidates = self.document_ids_in_collection(collection_id)
        if self._permission_reader is not None:
            prefiltered = self._permission_reader.readable_document_ids(principal, candidates)
            readable = frozenset(
                document_id
                for document_id in prefiltered
                if self._permission_reader.can_read_document(principal, document_id)
            )
        elif self._documents is not None:
            readable = frozenset(
                document_id
                for document_id in candidates
                if self._default_document_access(principal, document_id)
            )
        else:
            readable = candidates
        return tuple(sorted(readable, key=str))

    def _default_document_access(self, principal: Principal, document_id: Identifier) -> bool:
        if self._documents is None:
            return True
        access = self._documents.document_access(document_id)
        return access is not None and (
            principal.user_id == access.owner_user_id or access.visibility == "PUBLIC"
        )

    def add_document(
        self, principal: Principal, collection_id: int, document_id: Identifier
    ) -> None:
        with self._lock:
            collection = self._active(collection_id)
            mapping = (collection_id, document_id)
        self._require(principal, collection, "WRITE")
        if self._documents is not None and self._documents.document_access(document_id) is None:
            self._fail("DOCUMENT-001")
        with self._lock:
            with self._store.transaction():
                self._store.lock_collections((collection_id,))
                self._active(collection_id)
                if self._store.has_mapping(*mapping):
                    self._fail("COLLECTION-002")
                self._store.add_mapping(*mapping)
                if self._invalidate_document_cache is not None:
                    self._invalidate_document_cache(document_id)

    def remove_document(
        self, principal: Principal, collection_id: int, document_id: Identifier
    ) -> None:
        with self._lock:
            collection = self._active(collection_id)
            self._require_owner(principal, collection)
            mapping = (collection_id, document_id)
            if not self._store.has_mapping(*mapping):
                self._fail("COLLECTION-003")
        with self._lock:
            with self._store.transaction():
                self._store.lock_collections((collection_id,))
                collection = self._active(collection_id)
                self._require_owner(principal, collection)
                if not self._store.has_mapping(*mapping):
                    self._fail("COLLECTION-003")
                self._store.remove_mapping(*mapping)
                if self._invalidate_document_cache is not None:
                    self._invalidate_document_cache(document_id)

    def update_visibility(
        self, principal: Principal, collection_id: int, visibility: str
    ) -> None:
        with self._lock:
            with self._store.transaction():
                self._store.lock_collections((collection_id,))
                collection = self._active(collection_id)
                self._require_owner(principal, collection)
                if visibility not in EDIT_VISIBILITIES:
                    self._fail("COLLECTION-004")
                self._store.set_visibility(collection_id, visibility)

    def delete(self, principal: Principal, collection_id: int) -> None:
        if self._permission_cleanup is not None:
            self._permission_cleanup.execute_collection_delete(
                self._delete_plan(principal, collection_id)
            )
            return
        with self._delete_plan(principal, collection_id) as plan:
            if self._invalidate_document_cache is not None:
                for document_id in sorted(plan.document_ids, key=str):
                    self._invalidate_document_cache(document_id)
            if self._cleanup_collection_permissions is not None:
                self._cleanup_collection_permissions(plan.collection_ids)
            plan.apply()

    @contextmanager
    def _delete_plan(self, principal: Principal, collection_id: int):
        with self._lock:
            with self._store.transaction():
                root = self._active(collection_id)
                self._require_owner(principal, root)
                collection_ids = self._descendant_ids(collection_id)
                self._store.lock_collections(tuple(sorted(collection_ids)))
                root = self._active(collection_id)
                self._require_owner(principal, root)
                document_ids = self._store.document_ids_in_collections(collection_ids)

                def apply() -> None:
                    self._store.remove_mappings(collection_ids)
                    for item_id in collection_ids:
                        self._store.set_status(item_id, "DELETED")

                yield _DeletePlan(tuple(sorted(collection_ids)), document_ids, apply)

    def _descendant_ids(self, root_id: int) -> set[int]:
        selected: set[int] = set()
        pending = [root_id]
        while pending:
            current = pending.pop()
            if current in selected:
                continue
            selected.add(current)
            pending.extend(
                item.id
                for item in self._store.collections()
                if item.status != "DELETED" and item.parent_id == current
            )
        return selected

    def collection_access(
        self, collection_id: Identifier, *, include_deleted: bool = False
    ) -> ResourceAccess | None:
        with self._lock:
            collection = self._store.collection(collection_id)
            if collection is None or (collection.status == "DELETED" and not include_deleted):
                return None
            return ResourceAccess(
                collection.id,
                collection.owner_user_id,
                collection.visibility,
                collection.status,
            )

    def collection_ids_for_document(self, document_id: Identifier) -> frozenset[Identifier]:
        with self._lock:
            return frozenset(
                collection_id
                for collection_id in self._store.collection_ids_for_document(document_id)
                if self._store.collection(collection_id).status != "DELETED"  # type: ignore[union-attr]
            )

    def document_ids_in_collection(self, collection_id: Identifier) -> frozenset[Identifier]:
        with self._lock:
            return self._store.document_ids_in_collection(collection_id)
