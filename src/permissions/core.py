"""Permission ledger, effective access, and user access-cache projection."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from src.shared import (
    CollectionAccessCatalog,
    DocumentAccessCatalog,
    Identifier,
    McpDocumentAccess,
    PermissionSyncOutbox,
    Principal,
    PublicError,
    ResourceAccess,
    SyncEventRecord,
)
from src.shared.access import (
    CachedPermissionGrant,
    DirectPermissionRecord,
    InMemoryPermissionLedgerStore,
    PermissionLedgerStore,
    PermissionMapView,
)

from .cleanup import (
    cleanup_collection_permissions as _cleanup_collection_permissions,
    execute_collection_delete as _execute_collection_delete,
)
from .fallback import NoCollections
from .binding import PermissionBindingMixin

PERMISSION_KINDS = frozenset({"READ", "WRITE", "ADMIN"})
TARGET_TYPES = frozenset({"USER", "DEPARTMENT", "ROLE"})
RESOURCE_KINDS = frozenset({"DOCUMENT", "COLLECTION"})
LEVEL = {"READ": 1, "WRITE": 2, "ADMIN": 3}
SOURCE_ORDER = ("OWNER", "PUBLIC", "USER_CACHE", "ROLE", "DEPARTMENT")
PERMISSION_GRANT_ACTION = "GRANT"
PERMISSION_REVOKE_ACTION = "REVOKE"
DirectPermission = DirectPermissionRecord


@dataclass(frozen=True, slots=True)
class EffectivePermission:
    can_read: bool
    can_write: bool
    can_admin: bool
    sources: tuple[str, ...]


_CachedGrant = CachedPermissionGrant


class PermissionService(PermissionBindingMixin):
    """In-memory relational behavior behind the shared permission contracts."""

    def __init__(
        self,
        documents: DocumentAccessCatalog,
        collections: CollectionAccessCatalog | None = None,
        *,
        sync_outbox: PermissionSyncOutbox | None = None,
        clock: Callable[[], datetime] | None = None,
        store: PermissionLedgerStore | None = None,
    ) -> None:
        self.documents = documents
        self.collections = collections or NoCollections()
        self._sync_outbox = sync_outbox
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = store if store is not None else InMemoryPermissionLedgerStore()
        self._permission_map_view = PermissionMapView(self._store)
        self._lock = RLock()

    @property
    def _permissions(self):
        return getattr(self._store, "permissions", self._permission_map_view)

    @property
    def _user_cache(self):
        cache = getattr(self._store, "user_cache", None)
        if cache is not None:
            return cache
        return {True: True} if self._store.has_cached_grants() else {}

    @property
    def _next_id(self):
        return self._store.next_id  # type: ignore[attr-defined]

    @contextmanager
    def _transaction(self):
        """Commit permission rows, cache changes, IDs, and outbox together."""

        with self._lock:
            with self._store.transaction():
                outbox_transaction = (
                    self._sync_outbox.transaction()
                    if self._sync_outbox is not None
                    else nullcontext()
                )
                with outbox_transaction:
                    yield

    @staticmethod
    def _event_source(resource_kind: str) -> str:
        return f"DIRECT_{resource_kind}_PERMISSION"

    def _publish(self, permission: DirectPermission, action: str) -> None:
        if self._sync_outbox is None:
            return
        source = self._event_source(permission.resource_kind)
        self._sync_outbox.publish_permission_cache_refresh(
            source,
            permission.permission_id,
            action,
            payload={
                "source": source,
                "permissionId": permission.permission_id,
                "action": action,
            },
            occurred_at=self._now(),
        )

    def commit_sync_permission_refresh(
        self,
        event: SyncEventRecord,
        mark_processed: Callable[[], None],
    ) -> None:
        """Rebuild one permission projection in the event completion UoW."""

        payload = event.payload
        if (
            event.aggregate_type != "PERMISSION"
            or event.aggregate_version is not None
            or event.event_type != "PERMISSION_CACHE_REFRESH_REQUESTED"
            or not isinstance(event.aggregate_id, int)
            or isinstance(event.aggregate_id, bool)
            or event.aggregate_id < 1
            or not isinstance(payload, dict)
            or payload.get("permissionId") != event.aggregate_id
            or payload.get("source") not in {
                "DIRECT_DOCUMENT_PERMISSION", "DIRECT_COLLECTION_PERMISSION"
            }
            or payload.get("action") not in {
                PERMISSION_GRANT_ACTION, PERMISSION_REVOKE_ACTION
            }
        ):
            raise PublicError("SYNC-003")

        permission_id, source = event.aggregate_id, str(payload["source"])
        with self._lock, self._store.transaction():
            permission = self._store.permission(permission_id)
            if permission is not None and source != self._event_source(
                permission.resource_kind
            ):
                raise PublicError("SYNC-003")
            self._invalidate_permission(permission_id)
            if payload["action"] == PERMISSION_GRANT_ACTION and permission is not None:
                self._project(permission)
            mark_processed()

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _active(self, permission: DirectPermission | _CachedGrant) -> bool:
        expires_at = permission.expires_at
        if expires_at is None:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at > self._now()

    @staticmethod
    def _validate_target(
        target_type: str,
        user_id: int | None,
        department_id: int | None,
        role_code: str | None,
    ) -> None:
        values = {"USER": user_id, "DEPARTMENT": department_id, "ROLE": role_code}
        if (
            target_type not in TARGET_TYPES
            or values[target_type] is None
            or any(value is not None for key, value in values.items() if key != target_type)
        ):
            raise PublicError("PERMISSION-001")
        if target_type == "ROLE" and role_code == "USER":
            raise PublicError("PERMISSION-004")

    def _resource(self, resource_kind: str, resource_id: Identifier) -> ResourceAccess | None:
        if resource_kind == "DOCUMENT":
            return self.documents.document_access(resource_id, include_deleted=True)
        if resource_kind == "COLLECTION":
            return self.collections.collection_access(resource_id, include_deleted=True)
        raise PublicError("COMMON-002")

    def _require_resource(self, resource_kind: str, resource_id: Identifier) -> ResourceAccess:
        resource = self._resource(resource_kind, resource_id)
        if resource is None or resource.status == "DELETED":
            raise PublicError(
                "DOCUMENT-001" if resource_kind == "DOCUMENT" else "COLLECTION-001"
            )
        return resource

    def _rows(self, resource_kind: str, resource_id: Identifier) -> tuple[DirectPermission, ...]:
        return self._store.permissions_for_resource(resource_kind, resource_id)

    def _document_rows(self, document_id: Identifier) -> tuple[DirectPermission, ...]:
        collection_ids = self.collections.collection_ids_for_document(document_id)
        return self._store.permissions_for_document(document_id, collection_ids)

    @staticmethod
    def _matches(principal: Principal, permission: DirectPermission) -> bool:
        if permission.target_type == "USER":
            return principal.user_id is not None and permission.user_id == principal.user_id
        if permission.target_type == "DEPARTMENT":
            return (
                principal.department_id is not None
                and permission.department_id == principal.department_id
            )
        return permission.role_code in principal.roles

    def _project(self, permission: DirectPermission) -> None:
        if permission.target_type != "USER" or permission.user_id is None or not self._active(permission):
            return
        if permission.resource_kind == "DOCUMENT":
            document_ids = (permission.resource_id,)
        else:
            document_ids = self.collections.document_ids_in_collection(permission.resource_id)
        cached = _CachedGrant(
            permission.permission_id, permission.permission_kind, permission.expires_at
        )
        for document_id in document_ids:
            self._store.put_cached_grant(permission.user_id, document_id, cached)

    def _invalidate_permission(self, permission_id: int) -> None:
        self._store.invalidate_permission_cache(permission_id)

    def invalidate_document_cache(self, document_id: Identifier) -> None:
        with self._lock:
            self._store.invalidate_document_cache(document_id)

    def _cached_user_grants(
        self, principal: Principal, document_id: Identifier, rows: tuple[DirectPermission, ...]
    ) -> tuple[_CachedGrant, ...]:
        if principal.user_id is None:
            return ()
        grants = self._store.cached_grants(principal.user_id, document_id)
        if grants is None:
            for permission in rows:
                if permission.target_type == "USER" and self._matches(principal, permission):
                    self._project(permission)
            grants = self._store.cached_grants(principal.user_id, document_id) or ()
        active = tuple(grant for grant in grants if self._active(grant))
        if len(active) != len(grants):
            self._store.replace_cached_grants(
                principal.user_id, document_id, active
            )
        return active

    @staticmethod
    def _effective(grants: Iterable[tuple[str, str]]) -> EffectivePermission:
        materialized = tuple(grants)
        maximum = max((LEVEL[kind] for kind, _ in materialized), default=0)
        sources = tuple(
            source
            for source in SOURCE_ORDER
            if any(candidate == source for _, candidate in materialized)
        )
        return EffectivePermission(maximum >= 1, maximum >= 2, maximum >= 3, sources)

    def _effective_document(
        self, principal: Principal, resource: ResourceAccess, *, live_user: bool
    ) -> EffectivePermission:
        if principal.user_id is not None and principal.user_id == resource.owner_user_id:
            return EffectivePermission(True, True, True, ("OWNER",))

        rows = tuple(permission for permission in self._document_rows(resource.resource_id) if self._active(permission))
        grants: list[tuple[str, str]] = []
        if resource.visibility == "PUBLIC":
            grants.append(("READ", "PUBLIC"))

        if live_user:
            grants.extend(
                (permission.permission_kind, "USER_CACHE")
                for permission in rows
                if permission.target_type == "USER" and self._matches(principal, permission)
            )
        else:
            grants.extend(
                (permission.permission_kind, "USER_CACHE")
                for permission in self._cached_user_grants(
                    principal, resource.resource_id, rows
                )
            )

        grants.extend(
            (permission.permission_kind, permission.target_type)
            for permission in rows
            if permission.target_type in {"ROLE", "DEPARTMENT"}
            and self._matches(principal, permission)
        )
        return self._effective(grants)

    def _effective_collection(
        self, principal: Principal, resource: ResourceAccess
    ) -> EffectivePermission:
        if principal.user_id is not None and principal.user_id == resource.owner_user_id:
            return EffectivePermission(True, True, True, ("OWNER",))
        grants: list[tuple[str, str]] = []
        if resource.visibility == "PUBLIC":
            grants.append(("READ", "PUBLIC"))
        grants.extend(
            (
                permission.permission_kind,
                "USER_CACHE" if permission.target_type == "USER" else permission.target_type,
            )
            for permission in self._rows("COLLECTION", resource.resource_id)
            if self._active(permission) and self._matches(principal, permission)
        )
        return self._effective(grants)

    @staticmethod
    def _allows(effective: EffectivePermission, required: str) -> bool:
        if required == "READ":
            return effective.can_read
        if required == "WRITE":
            return effective.can_write
        if required == "ADMIN":
            return effective.can_admin
        raise PublicError("COMMON-002")

    def allows(
        self,
        principal: Principal,
        resource_kind: str,
        resource_id: Identifier,
        required: str,
    ) -> bool:
        with self._lock:
            resource = self._resource(resource_kind, resource_id)
            if resource is None or resource.status == "DELETED":
                return False
            effective = (
                self._effective_document(principal, resource, live_user=False)
                if resource_kind == "DOCUMENT"
                else self._effective_collection(principal, resource)
            )
            return self._allows(effective, required)

    def collection_decider(self, principal: Principal, collection: object, required: str) -> bool:
        return self.allows(principal, "COLLECTION", getattr(collection, "id"), required)

    def document_decider(self, principal: Principal, document: object, required: str) -> bool:
        return self.allows(principal, "DOCUMENT", getattr(document, "id"), required)

    def grant(
        self,
        actor: Principal,
        resource_kind: str,
        resource_id: Identifier,
        permission_kind: str,
        *,
        target_type: str,
        user_id: int | None = None,
        department_id: int | None = None,
        role_code: str | None = None,
        expires_at: datetime | None = None,
    ) -> DirectPermission:
        with self._transaction():
            if resource_kind not in RESOURCE_KINDS or permission_kind not in PERMISSION_KINDS:
                raise PublicError("COMMON-002")
            self._store.lock_resource(resource_kind, resource_id)
            self._require_resource(resource_kind, resource_id)
            if not self.allows(actor, resource_kind, resource_id, "ADMIN"):
                raise PublicError("ROLE-002")
            self._validate_target(target_type, user_id, department_id, role_code)
            permission = self._store.create_permission(
                resource_kind,
                resource_id,
                permission_kind,
                target_type,
                user_id,
                department_id,
                role_code,
                expires_at,
            )
            self._project(permission)
            self._publish(permission, PERMISSION_GRANT_ACTION)
            return permission

    def list_direct(
        self, actor: Principal, resource_kind: str, resource_id: Identifier
    ) -> tuple[DirectPermission, ...]:
        with self._lock:
            self._require_resource(resource_kind, resource_id)
            if not self.allows(actor, resource_kind, resource_id, "ADMIN"):
                raise PublicError("ROLE-002")
            return tuple(sorted(self._rows(resource_kind, resource_id), key=lambda item: item.permission_id))

    def revoke(
        self,
        actor: Principal,
        resource_kind: str,
        resource_id: Identifier,
        permission_id: int,
    ) -> None:
        with self._transaction():
            if resource_kind in RESOURCE_KINDS:
                self._store.lock_resource(resource_kind, resource_id)
            self._require_resource(resource_kind, resource_id)
            if not self.allows(actor, resource_kind, resource_id, "ADMIN"):
                raise PublicError("ROLE-002")
            self._store.lock_permission(permission_id)
            permission = self._store.permission(permission_id)
            if (
                permission is None
                or permission.resource_kind != resource_kind
                or permission.resource_id != resource_id
            ):
                raise PublicError(
                    "PERMISSION-003" if resource_kind == "DOCUMENT" else "PERMISSION-002"
                )
            self._invalidate_permission(permission_id)
            self._store.delete_permission(permission_id)
            self._publish(permission, PERMISSION_REVOKE_ACTION)

    def cleanup_collection_permissions(self, collection_ids: Iterable[Identifier]) -> None:
        _cleanup_collection_permissions(self, collection_ids)

    def execute_collection_delete(self, plan) -> None:
        _execute_collection_delete(self, plan)

    def effective_document(
        self, principal: Principal, document_id: Identifier
    ) -> EffectivePermission:
        with self._lock:
            resource = self.documents.document_access(document_id, include_deleted=True)
            if resource is None or resource.status == "DELETED":
                raise PublicError("DOCUMENT-001")
            return self._effective_document(principal, resource, live_user=False)

    def effective_collection(
        self, principal: Principal, collection_id: Identifier
    ) -> EffectivePermission:
        with self._lock:
            resource = self.collections.collection_access(collection_id, include_deleted=True)
            if resource is None or resource.status == "DELETED":
                raise PublicError("COLLECTION-001")
            return self._effective_collection(principal, resource)

    def _readable_ids(
        self,
        principal: Principal,
        candidate_ids: Iterable[Identifier],
        *,
        live_user: bool,
    ) -> frozenset[Identifier]:
        """Decide a whole candidate set inside one ambient transaction.

        Each document costs several ledger reads, and outside a transaction every
        one of those opens its own connection.  Deciding the set together keeps
        the cost proportional to the queries instead of to the connections.
        """
        with self._lock, self._store.transaction():
            readable: set[Identifier] = set()
            for document_id in dict.fromkeys(candidate_ids):
                resource = self.documents.document_access(document_id, include_deleted=True)
                if (
                    resource is not None
                    and resource.status != "DELETED"
                    and self._effective_document(
                        principal, resource, live_user=live_user
                    ).can_read
                ):
                    readable.add(document_id)
            return frozenset(readable)

    def readable_document_ids(
        self, principal: Principal, candidate_ids: Iterable[Identifier]
    ) -> frozenset[Identifier]:
        """Cached prefilter; search must still call the live method on candidates."""
        return self._readable_ids(principal, candidate_ids, live_user=False)

    def live_readable_document_ids(
        self, principal: Principal, candidate_ids: Iterable[Identifier]
    ) -> frozenset[Identifier]:
        """Batched can_read_document, with the same live-ledger decision."""
        return self._readable_ids(principal, candidate_ids, live_user=True)

    def can_read_document(self, principal: Principal, document_id: Identifier) -> bool:
        """Live ledger decision used after retrieval and on every cache hit."""
        return document_id in self._readable_ids(
            principal, (document_id,), live_user=True
        )

    def mcp_document_access(
        self, principal: Principal, document_id: Identifier
    ) -> McpDocumentAccess:
        """Authorize without exposing whether an unreadable identifier exists."""
        with self._lock:
            resource = self.documents.document_access(
                document_id, include_deleted=True
            )
            if resource is None:
                return McpDocumentAccess.DENIED
            allowed = self._effective_document(
                principal, resource, live_user=True
            ).can_read
            if not allowed:
                return McpDocumentAccess.DENIED
            if resource.status == "DELETED":
                return McpDocumentAccess.DELETED
            return McpDocumentAccess.ALLOWED
