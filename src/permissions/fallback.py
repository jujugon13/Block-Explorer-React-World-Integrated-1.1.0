"""Empty collection catalog used until the composition root binds one."""

from __future__ import annotations

from src.shared import Identifier, ResourceAccess


class NoCollections:
    def collection_access(
        self, collection_id: Identifier, *, include_deleted: bool = False
    ) -> ResourceAccess | None:
        del collection_id, include_deleted
        return None

    def collection_ids_for_document(
        self, document_id: Identifier
    ) -> frozenset[Identifier]:
        del document_id
        return frozenset()

    def document_ids_in_collection(
        self, collection_id: Identifier
    ) -> frozenset[Identifier]:
        del collection_id
        return frozenset()
