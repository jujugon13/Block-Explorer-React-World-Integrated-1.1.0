"""Safe delayed cleanup for S3 candidates left before the DB commit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from src.shared import (
    ConsistencyFinding,
    ObjectInventory,
    ObjectReferenceCatalog,
    StoredObject,
)


_EXTENSIONS = frozenset({"pdf", "docx", "txt", "md"})


def _is_candidate(item: StoredObject, prefix: str) -> bool:
    key = item.location.key
    if not key.startswith(prefix):
        return False
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "documents" or "." not in parts[2]:
        return False
    stem, extension = parts[2].rsplit(".", 1)
    if extension.casefold() not in _EXTENSIONS:
        return False
    try:
        UUID(parts[1])
        UUID(stem)
    except ValueError:
        return False
    return True


def _finding(item: StoredObject, *, resolved: bool = False) -> ConsistencyFinding:
    location = item.location
    fingerprint = uuid5(
        NAMESPACE_URL,
        ":".join(
            (
                "vectorshelf-orphan",
                location.provider,
                location.namespace,
                location.key,
                item.last_modified.isoformat(),
            )
        ),
    )
    return ConsistencyFinding(
        "ORPHANED_DATA",
        "WARNING",
        issue_id=str(fingerprint),
        resolved=resolved,
    )


class OrphanedDataDetector:
    """Report or delete only aged, unreferenced document candidate objects."""

    def __init__(
        self,
        inventory: ObjectInventory,
        references: ObjectReferenceCatalog,
        *,
        clock: Callable[[], datetime] | None = None,
        grace: timedelta = timedelta(hours=24),
        prefix: str = "documents/",
    ) -> None:
        if grace < timedelta(0):
            raise ValueError("orphan grace must not be negative")
        if not prefix.startswith("documents/"):
            raise ValueError("orphan prefix must be below documents/")
        self.inventory = inventory
        self.references = references
        self._clock = clock or (lambda: datetime.now(UTC))
        self.grace = grace
        self.prefix = prefix

    def detect(
        self,
        *,
        cursor: str | None,
        mode: str,
        limit: int,
    ) -> tuple[ConsistencyFinding, ...]:
        if mode not in {"DRY_RUN", "REPAIR"}:
            raise ValueError("invalid reconciliation mode")
        if limit < 1:
            raise ValueError("reconciliation limit must be positive")
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        cutoff = now - self.grace
        findings: list[ConsistencyFinding] = []
        processed = 0
        for item in self.inventory.iter_objects(self.prefix):
            if cursor is not None and item.location.key <= cursor:
                continue
            modified = item.last_modified
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            if not _is_candidate(item, self.prefix) or modified > cutoff:
                continue
            if self.references.is_referenced(item.location):
                findings.append(_finding(item, resolved=True))
                continue
            if mode == "REPAIR":
                if self.references.is_referenced(item.location):
                    findings.append(_finding(item, resolved=True))
                    continue
                self.inventory.delete(item.location)
                findings.append(_finding(item, resolved=True))
            else:
                findings.append(_finding(item))
            processed += 1
            if processed >= limit:
                break
        return tuple(findings)
