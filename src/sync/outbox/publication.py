"""Transactional outbox publication and structural idempotency."""

from __future__ import annotations

import json
from datetime import datetime

from src.shared import Identifier

from ..model import SyncEventRow


def _json_equal(left: object, right: object) -> bool:
    """Compare normalized JSON values by their structural JSON meaning."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return type(left) is type(right) and left == right


class PublicationControls:
    """Mixin for durable publication without changing the service API."""

    def _json(self, payload: object) -> tuple[str, object]:
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            self._fail("COMMON-002")
        return canonical, json.loads(canonical)

    def publish(
        self,
        *,
        idempotency_key: str,
        aggregate_type: str,
        aggregate_id: Identifier,
        aggregate_version: int | None,
        event_type: str,
        payload: object,
        occurred_at: datetime | None = None,
        max_retries: int | None = None,
    ) -> SyncEventRow:
        moment = self._now(occurred_at)
        canonical, stored_payload = self._json(payload)
        retries = self.default_max_retries if max_retries is None else max_retries
        if retries < 0:
            self._fail("COMMON-002")
        with self.store.transaction():
            existing = self.store.get_event_by_key(idempotency_key)
            if existing is None:
                event = SyncEventRow(
                    self._id(),
                    idempotency_key,
                    aggregate_type,
                    aggregate_id,
                    aggregate_version,
                    event_type,
                    stored_payload,
                    canonical,
                    "PENDING",
                    moment,
                    moment,
                    retries,
                )
                if self.store.insert_event(event):
                    return event
                existing = self.store.get_event_by_key(
                    idempotency_key, for_update=True
                )
                if existing is None:
                    self._fail("SYNC-003")
            same = (
                existing.aggregate_type == aggregate_type
                and existing.aggregate_id == aggregate_id
                and existing.aggregate_version == aggregate_version
                and existing.event_type == event_type
                and _json_equal(existing.payload, stored_payload)
            )
            if not same:
                self._fail("SYNC-003")
            return existing

    def publish_document_version_created(
        self,
        version_id: Identifier,
        version_no: int,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ) -> SyncEventRow:
        return self.publish(
            idempotency_key=(
                f"DOCUMENT_VERSION:{version_id}:DOCUMENT_VERSION_CREATED:{version_no}"
            ),
            aggregate_type="DOCUMENT_VERSION",
            aggregate_id=version_id,
            aggregate_version=version_no,
            event_type="DOCUMENT_VERSION_CREATED",
            payload=payload,
            occurred_at=occurred_at,
        )

    def publish_document_deleted(
        self,
        document_id: Identifier,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ) -> SyncEventRow:
        return self.publish(
            idempotency_key=f"DOCUMENT:{document_id}:DOCUMENT_DELETED",
            aggregate_type="DOCUMENT",
            aggregate_id=document_id,
            aggregate_version=None,
            event_type="DOCUMENT_DELETED",
            payload=payload,
            occurred_at=occurred_at,
        )

    def publish_permission_cache_refresh(
        self,
        source: str,
        permission_id: Identifier,
        action: str,
        *,
        payload: object,
        occurred_at: datetime | None = None,
    ) -> SyncEventRow:
        return self.publish(
            idempotency_key=(
                f"PERMISSION:{source}:{permission_id}:"
                f"PERMISSION_CACHE_REFRESH_REQUESTED:{action}"
            ),
            aggregate_type="PERMISSION",
            aggregate_id=permission_id,
            aggregate_version=None,
            event_type="PERMISSION_CACHE_REFRESH_REQUESTED",
            payload=payload,
            occurred_at=occurred_at,
        )
