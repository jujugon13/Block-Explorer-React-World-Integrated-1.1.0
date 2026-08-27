"""Cloud S3 storage; bucket creation remains an external responsibility."""

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


class S3Storage:
    provider = "s3"

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
            if error.missing or error.status_code == 404:
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
            if error.missing or error.status_code == 404:
                return
            raise StorageUnavailable from None
        except Exception:
            raise StorageUnavailable from None
