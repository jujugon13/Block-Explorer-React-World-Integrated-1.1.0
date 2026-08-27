"""Late-bound ports completed by the process composition root."""

from __future__ import annotations


class DocumentBindingMixin:
    def bind_indexing(self, indexing) -> None:
        with self._read():
            if self._indexing is indexing:
                return
            if self._indexing is not None:
                raise RuntimeError("document workspace is already bound to indexing")
            if self._store.has_documents():
                raise RuntimeError("indexing must be bound before document creation")
            self._indexing = indexing
            indexing.bind_document_ledger(self)

    def bind_sync_outbox(self, outbox) -> None:
        with self._read():
            if self._sync_outbox is not None and self._sync_outbox is not outbox:
                raise RuntimeError("document workspace is already bound to another outbox")
            self._sync_outbox = outbox

    def bind_permissions(self, service: object) -> None:
        decider = getattr(service, "document_decider", None)
        if not callable(decider):
            raise TypeError("permission service does not implement document decisions")
        with self._read():
            if self._access_decider != self._default_access and self._access_decider != decider:
                raise RuntimeError("documents already use another access decider")
            self._access_decider = decider
