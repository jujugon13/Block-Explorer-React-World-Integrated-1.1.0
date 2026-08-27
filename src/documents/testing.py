from __future__ import annotations

from src.shared import StorageLocation, StorageObjectNotFound


class MemoryStorage:
    provider = "local"
    namespace = "vectorshelf"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_count = 0
        self.get_count = 0
        self.deleted: list[str] = []

    def ensure_location(self, location: StorageLocation) -> None:
        if location.provider != self.provider or location.namespace != self.namespace:
            from src.shared import StorageLocationMismatch

            raise StorageLocationMismatch

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation:
        if len(data) != expected_size:
            raise AssertionError("bad expected size")
        self.put_count += 1
        self.objects[key] = data
        return StorageLocation(self.provider, self.namespace, key, expected_size)

    def get(self, location: StorageLocation) -> bytes:
        self.ensure_location(location)
        self.get_count += 1
        try:
            return self.objects[location.key]
        except KeyError:
            raise StorageObjectNotFound from None

    def delete(self, location: StorageLocation) -> None:
        self.ensure_location(location)
        self.deleted.append(location.key)
        self.objects.pop(location.key, None)

