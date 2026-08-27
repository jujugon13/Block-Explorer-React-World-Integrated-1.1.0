"""S3-compatible storage that ensures its namespace before each write."""

from __future__ import annotations

from src.shared import (
    ObjectClient,
    ObjectClientError,
    ObjectMissingError,
    StorageLocation,
    StorageLocationMismatch,
    StorageObjectNotFound,
    StorageUnavailable,
)


class MinioStorage:
    provider = "minio"

    def __init__(self, namespace: str, client: ObjectClient) -> None:
        self.namespace = namespace
        self.client = client

    def ensure_location(self, location: StorageLocation) -> None:
        if location.provider != self.provider or location.namespace != self.namespace:
            raise StorageLocationMismatch

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation:
        if len(data) != expected_size:
            raise StorageUnavailable
        try:
            self.client.ensure_namespace(self.namespace)
            self.client.put(self.namespace, key, data)
        except Exception:
            raise StorageUnavailable from None
        return StorageLocation(self.provider, self.namespace, key, expected_size)

    def get(self, location: StorageLocation) -> bytes:
        self.ensure_location(location)
        try:
            data = self.client.get(self.namespace, location.key)
        except ObjectMissingError:
            raise StorageObjectNotFound from None
        except ObjectClientError as error:
            if error.missing:
                raise StorageObjectNotFound from None
            raise StorageUnavailable from None
        except Exception:
            raise StorageUnavailable from None
        if len(data) != location.size:
            raise StorageUnavailable
        return data

    def delete(self, location: StorageLocation) -> None:
        self.ensure_location(location)
        try:
            self.client.delete(self.namespace, location.key)
        except ObjectMissingError:
            return
        except ObjectClientError as error:
            if error.missing:
                return
            raise StorageUnavailable from None
        except Exception:
            raise StorageUnavailable from None
