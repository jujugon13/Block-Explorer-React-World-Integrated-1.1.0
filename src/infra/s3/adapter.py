"""AWS S3 implementation of the shared object-storage contract."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from src.shared import (
    StorageLocation,
    StorageLocationMismatch,
    StorageObjectNotFound,
    StorageUnavailable,
    StoredObject,
)


BUCKET_ENVIRONMENT_KEY = "S3_BUCKET"
_MISSING_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NotFound"})


class S3ConfigurationError(ValueError):
    """Required deployment configuration is absent or malformed."""


class S3DependencyError(RuntimeError):
    """The configured AWS SDK is unavailable."""


class S3CompatibilityError(RuntimeError):
    """The configured endpoint or bucket cannot satisfy the contract."""


@dataclass(frozen=True, slots=True)
class S3Config:
    bucket: str = field(repr=False)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "S3Config":
        source = os.environ if environment is None else environment
        bucket = source.get(BUCKET_ENVIRONMENT_KEY, "").strip()
        if not bucket:
            raise S3ConfigurationError("S3_BUCKET is required")
        return cls(bucket)

    def __repr__(self) -> str:
        return "S3Config(<redacted>)"


def _is_missing(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    code = details.get("Code") if isinstance(details, Mapping) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return str(code) in _MISSING_CODES or str(status) == "404"


class S3Storage:
    """Cloud S3 storage; bucket creation remains an external responsibility."""

    provider = "s3"

    def __init__(self, namespace: str, client: Any) -> None:
        self.namespace = namespace
        self.client = client

    def ensure_location(self, location: StorageLocation) -> None:
        if location.provider != self.provider or location.namespace != self.namespace:
            raise StorageLocationMismatch

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation:
        if len(data) != expected_size:
            raise StorageUnavailable
        try:
            self.client.put_object(Bucket=self.namespace, Key=key, Body=data)
        except Exception:
            raise StorageUnavailable from None
        return StorageLocation(self.provider, self.namespace, key, expected_size)

    def get(self, location: StorageLocation) -> bytes:
        self.ensure_location(location)
        body = None
        try:
            response = self.client.get_object(
                Bucket=self.namespace,
                Key=location.key,
            )
            body = response["Body"]
            data = body.read()
        except Exception as error:
            if _is_missing(error):
                raise StorageObjectNotFound from None
            raise StorageUnavailable from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if not isinstance(data, bytes) or len(data) != location.size:
            raise StorageUnavailable
        return data

    def delete(self, location: StorageLocation) -> None:
        self.ensure_location(location)
        try:
            self.client.delete_object(Bucket=self.namespace, Key=location.key)
        except Exception as error:
            if _is_missing(error):
                return
            raise StorageUnavailable from None

    def iter_objects(self, prefix: str) -> Iterator[StoredObject]:
        token: str | None = None
        while True:
            arguments: dict[str, object] = {
                "Bucket": self.namespace,
                "Prefix": prefix,
            }
            if token is not None:
                arguments["ContinuationToken"] = token
            try:
                response = self.client.list_objects_v2(**arguments)
                contents = response.get("Contents", ())
                if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes)):
                    raise TypeError("invalid S3 object listing")
                for item in contents:
                    if not isinstance(item, Mapping):
                        raise TypeError("invalid S3 object metadata")
                    key = item.get("Key")
                    size = item.get("Size")
                    modified = item.get("LastModified")
                    if (
                        not isinstance(key, str)
                        or isinstance(size, bool)
                        or not isinstance(size, int)
                        or size < 0
                        or not isinstance(modified, datetime)
                    ):
                        raise TypeError("invalid S3 object metadata")
                    yield StoredObject(
                        StorageLocation(self.provider, self.namespace, key, size),
                        modified,
                    )
                if not response.get("IsTruncated"):
                    return
                next_token = response.get("NextContinuationToken")
                if not isinstance(next_token, str) or not next_token:
                    raise TypeError("invalid S3 continuation token")
                token = next_token
            except Exception:
                raise StorageUnavailable from None


def _default_client():
    try:
        import boto3
    except ImportError:
        raise S3DependencyError("boto3 is required for AWS S3") from None

    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None or getattr(credentials, "method", None) != "env":
            raise S3ConfigurationError("AWS environment credentials are required")
        if not session.region_name:
            raise S3ConfigurationError("AWS region is required")
        return session.client("s3")
    except S3ConfigurationError:
        raise
    except Exception:
        raise S3ConfigurationError("AWS environment configuration is invalid") from None


def _response_region(client: Any, bucket: str, response: Mapping[str, Any]) -> str:
    region = response.get("BucketRegion")
    metadata = response.get("ResponseMetadata", {})
    if not region and isinstance(metadata, Mapping):
        headers = metadata.get("HTTPHeaders", {})
        if isinstance(headers, Mapping):
            region = headers.get("x-amz-bucket-region")
    if not region:
        location = client.get_bucket_location(Bucket=bucket).get("LocationConstraint")
        region = "us-east-1" if location in (None, "") else location
        if region == "EU":
            region = "eu-west-1"
    return str(region)


def verify_s3(storage: S3Storage) -> None:
    """Fail closed before exposing a storage instance to the application."""

    metadata = getattr(storage.client, "meta", None)
    endpoint = urlparse(str(getattr(metadata, "endpoint_url", "")))
    region = str(getattr(metadata, "region_name", ""))
    hostname = endpoint.hostname or ""
    if endpoint.scheme != "https" or not hostname.endswith(".amazonaws.com"):
        raise S3CompatibilityError("AWS S3 HTTPS endpoint is required")
    if not region:
        raise S3CompatibilityError("S3 client region is unavailable")
    try:
        response = storage.client.head_bucket(Bucket=storage.namespace)
        observed_region = _response_region(storage.client, storage.namespace, response)
    except Exception:
        raise S3CompatibilityError("S3 bucket preflight failed") from None
    if observed_region != region:
        raise S3CompatibilityError("S3 bucket region does not match client region")


def build_s3_storage(
    config: S3Config | None = None,
    *,
    client: Any | None = None,
    verify: bool = True,
) -> S3Storage:
    selected = config if config is not None else S3Config.from_env()
    storage = S3Storage(selected.bucket, client if client is not None else _default_client())
    if verify:
        verify_s3(storage)
    return storage
