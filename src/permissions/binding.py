"""Composition-time binding for permission catalogs and the durable outbox."""

from __future__ import annotations


class PermissionBindingMixin:
    def bind_catalogs(self, documents, collections) -> None:
        with self._lock:
            if self._permissions or self._user_cache:
                if self.documents is not documents or self.collections is not collections:
                    raise RuntimeError("permission catalogs cannot change after use")
                return
            self.documents = documents
            self.collections = collections

    def bind_sync_outbox(self, outbox) -> None:
        with self._lock:
            if self._sync_outbox is not None and self._sync_outbox is not outbox:
                raise RuntimeError("permission service is already bound to another outbox")
            self._sync_outbox = outbox
