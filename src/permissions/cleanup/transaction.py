"""Transaction participant used by recursive collection deletion."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

from src.shared import CollectionDeletePlan, Identifier


def _cleanup(
    service: Any,
    collection_ids: Iterable[Identifier],
    document_ids: Iterable[Identifier],
) -> None:
    identifiers = frozenset(collection_ids)
    for document_id in sorted(frozenset(document_ids), key=str):
        service.invalidate_document_cache(document_id)
    permissions = sorted(
        (
            permission
            for permission in service._permissions.values()
            if permission.resource_kind == "COLLECTION"
            and permission.resource_id in identifiers
        ),
        key=lambda permission: permission.permission_id,
    )
    for permission in permissions:
        service._invalidate_permission(permission.permission_id)
        service._permissions.pop(permission.permission_id, None)
        service._publish(permission, "REVOKE")


def cleanup_collection_permissions(
    service: Any, collection_ids: Iterable[Identifier]
) -> None:
    with service._transaction():
        _cleanup(service, collection_ids, ())


def execute_collection_delete(
    service: Any,
    plan_context: AbstractContextManager[CollectionDeletePlan],
) -> None:
    # Permission methods also acquire the collection catalog lock. Use the
    # same permission -> collection order here to prevent delete/grant deadlock.
    with service._lock:
        with plan_context as plan:
            with service._transaction():
                _cleanup(service, plan.collection_ids, plan.document_ids)
                plan.apply()
