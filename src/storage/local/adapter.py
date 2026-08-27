"""Atomic local-filesystem object storage."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TypeAlias

from src.shared import (
    StorageLocation,
    StorageLocationMismatch,
    StorageObjectNotFound,
    StorageUnavailable,
)

PathInput: TypeAlias = str | os.PathLike[str]


class LocalStorage:
    provider = "local"

    def __init__(self, root: PathInput, namespace: str) -> None:
        self.root = Path(root).resolve()
        self.namespace = namespace

    def ensure_location(self, location: StorageLocation) -> None:
        if location.provider != self.provider or location.namespace != self.namespace:
            raise StorageLocationMismatch

    def _path(self, key: str) -> Path:
        try:
            target = (self.root / key).resolve()
            target.relative_to(self.root)
        except (OSError, ValueError):
            raise StorageUnavailable from None
        if target == self.root:
            raise StorageUnavailable
        return target

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation:
        if len(data) != expected_size:
            raise StorageUnavailable

        target = self._path(key)
        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
            if temporary.stat().st_size != expected_size:
                raise StorageUnavailable
            os.replace(temporary, target)
            temporary = None
            return StorageLocation(self.provider, self.namespace, key, expected_size)
        except StorageUnavailable:
            raise
        except OSError:
            raise StorageUnavailable from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, location: StorageLocation) -> bytes:
        self.ensure_location(location)
        try:
            data = self._path(location.key).read_bytes()
        except FileNotFoundError:
            raise StorageObjectNotFound from None
        except OSError:
            raise StorageUnavailable from None
        if len(data) != location.size:
            raise StorageUnavailable
        return data

    def delete(self, location: StorageLocation) -> None:
        self.ensure_location(location)
        try:
            self._path(location.key).unlink(missing_ok=True)
        except OSError:
            raise StorageUnavailable from None
