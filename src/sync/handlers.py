"""Confirmed sync-event handlers and their event-type registry."""

from __future__ import annotations

from collections.abc import Callable

from src.shared import DocumentAccessCatalog, PublicError, SyncEventRecord
from src.shared.sync import SyncIndexingEffects, SyncPermissionEffects


SyncHandler = Callable[[SyncEventRecord, Callable[[], None]], None]


class SyncHandlerRegistry:
    """Route an outbox record to exactly one registered transactional handler."""

    def __init__(self, handlers: dict[str, SyncHandler] | None = None) -> None:
        self._handlers = dict(handlers or {})

    def register(self, event_type: str, handler: SyncHandler) -> None:
        self._handlers[event_type] = handler

    def commit(
        self,
        event: SyncEventRecord,
        mark_processed: Callable[[], None],
    ) -> None:
        handler = self._handlers.get(event.event_type)
        if handler is None:
            raise PublicError("SYNC-003")
        handler(event, mark_processed)


class DocumentDeletedHandler:
    """Validate a deletion event and atomically stale its active vectors."""

    def __init__(
        self,
        documents: DocumentAccessCatalog,
        indexing: SyncIndexingEffects,
    ) -> None:
        self._documents = documents
        self._indexing = indexing

    def __call__(
        self,
        event: SyncEventRecord,
        mark_processed: Callable[[], None],
    ) -> None:
        payload = event.payload
        if (
            event.aggregate_type != "DOCUMENT"
            or event.aggregate_version is not None
            or not isinstance(payload, dict)
            or payload.get("documentId") != event.aggregate_id
        ):
            raise PublicError("SYNC-003")
        document = self._documents.document_access(
            event.aggregate_id,
            include_deleted=True,
        )
        if document is None or document.status != "DELETED":
            raise PublicError("SYNC-003")
        self._indexing.commit_sync_document_deleted(
            event.aggregate_id,
            mark_processed,
        )


class DocumentVersionCreatedHandler:
    def __init__(self, indexing: SyncIndexingEffects) -> None:
        self._indexing = indexing

    def __call__(self, event: SyncEventRecord, mark_processed: Callable[[], None]) -> None:
        payload = event.payload
        if (
            event.aggregate_type != "DOCUMENT_VERSION"
            or not isinstance(event.aggregate_version, int)
            or event.aggregate_version < 1
            or not isinstance(payload, dict)
            or payload.get("versionId") != event.aggregate_id
            or not isinstance(payload.get("documentId"), (int, str))
        ):
            raise PublicError("SYNC-003")
        self._indexing.commit_sync_document_version_created(
            payload["documentId"],
            event.aggregate_id,
            event.aggregate_version,
            mark_processed,
        )


class DocumentReindexHandler:
    def __init__(self, indexing: SyncIndexingEffects) -> None:
        self._indexing = indexing

    def __call__(self, event: SyncEventRecord, mark_processed: Callable[[], None]) -> None:
        payload = event.payload
        if (
            event.aggregate_type != "DOCUMENT_VERSION"
            or not isinstance(payload, dict)
            or not isinstance(payload.get("modelId"), (int, str))
        ):
            raise PublicError("SYNC-003")
        self._indexing.commit_sync_document_reindex(
            event.aggregate_id,
            payload["modelId"],
            mark_processed,
        )


class EmbeddingModelActivatedHandler:
    def __init__(self, indexing: SyncIndexingEffects) -> None:
        self._indexing = indexing

    def __call__(self, event: SyncEventRecord, mark_processed: Callable[[], None]) -> None:
        if event.aggregate_type != "EMBEDDING_MODEL":
            raise PublicError("SYNC-003")
        self._indexing.commit_sync_model_activated(event.aggregate_id, mark_processed)


class PermissionRefreshHandler:
    """Delegate cache projection when configured; otherwise validate the event."""

    def __init__(self, effects: SyncPermissionEffects | None = None) -> None:
        self._effects = effects

    def __call__(self, event: SyncEventRecord, mark_processed: Callable[[], None]) -> None:
        if event.aggregate_type != "PERMISSION" or not isinstance(event.payload, dict):
            raise PublicError("SYNC-003")
        if self._effects is None:
            mark_processed()
            return
        self._effects.commit_sync_permission_refresh(event, mark_processed)


def indexing_handlers(
    indexing: SyncIndexingEffects,
    documents: DocumentAccessCatalog,
    permission_effects: SyncPermissionEffects | None = None,
) -> SyncHandlerRegistry:
    """Build handlers for every confirmed sync event type."""

    return SyncHandlerRegistry(
        {
            "DOCUMENT_VERSION_CREATED": DocumentVersionCreatedHandler(indexing),
            "DOCUMENT_REINDEX_REQUESTED": DocumentReindexHandler(indexing),
            "DOCUMENT_DELETED": DocumentDeletedHandler(documents, indexing),
            "PERMISSION_CACHE_REFRESH_REQUESTED": PermissionRefreshHandler(
                permission_effects
            ),
            "EMBEDDING_MODEL_ACTIVATED": EmbeddingModelActivatedHandler(indexing),
        }
    )
