"""Storage contracts shared by document producers and storage adapters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .errors import PublicError


@dataclass(frozen=True, slots=True)
class StorageLocation:
    provider: str
    namespace: str
    key: str
    size: int


@dataclass(frozen=True, slots=True)
class StoredObject:
    location: StorageLocation
    last_modified: datetime


class StorageUnavailable(PublicError):
    def __init__(self) -> None:
        super().__init__("DOCUMENT-STORAGE-001")


class StorageObjectNotFound(PublicError):
    def __init__(self) -> None:
        super().__init__("DOCUMENT-STORAGE-002")


class StorageLocationMismatch(PublicError):
    def __init__(self) -> None:
        super().__init__("DOCUMENT-STORAGE-003")


class ObjectClientError(Exception):
    """Normalized failure from an injected object-store client."""

    def __init__(
        self,
        message: str = "object client failure",
        *,
        status_code: int | None = None,
        missing: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.missing = missing


class ObjectMissingError(ObjectClientError):
    def __init__(self, message: str = "object not found") -> None:
        super().__init__(message, missing=True)


class ObjectClient(Protocol):
    """Small port supplied by the MinIO or cloud-S3 integration environment."""

    def ensure_namespace(self, namespace: str) -> None: ...

    def put(self, namespace: str, key: str, data: bytes) -> None: ...

    def get(self, namespace: str, key: str) -> bytes: ...

    def delete(self, namespace: str, key: str) -> None: ...


class ObjectStorage(Protocol):
    provider: str
    namespace: str

    def ensure_location(self, location: StorageLocation) -> None: ...

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation: ...

    def get(self, location: StorageLocation) -> bytes: ...

    def delete(self, location: StorageLocation) -> None: ...


class ObjectInventory(ObjectStorage, Protocol):
    def iter_objects(self, prefix: str) -> Iterable[StoredObject]: ...


class ObjectReferenceCatalog(Protocol):
    def is_referenced(self, location: StorageLocation) -> bool: ...
