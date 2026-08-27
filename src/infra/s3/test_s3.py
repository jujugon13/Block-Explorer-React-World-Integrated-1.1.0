from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from src.infra.s3 import (
    S3CompatibilityError,
    S3Config,
    S3ConfigurationError,
    S3Storage,
    build_s3_storage,
    verify_s3,
)
from src.shared import (
    StorageLocation,
    StorageLocationMismatch,
    StorageObjectNotFound,
    StorageUnavailable,
)


class _ClientFailure(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.closed = False

    def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self) -> None:
        self.meta = SimpleNamespace(
            endpoint_url="https://s3.test.amazonaws.com",
            region_name="test-region-1",
        )
        self.objects: dict[tuple[str, str], bytes] = {}
        self.modified: dict[tuple[str, str], datetime] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.failure: Exception | None = None
        self.page_size = 1000

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.calls.append(("put", Bucket, Key))
        if self.failure:
            raise self.failure
        self.objects[(Bucket, Key)] = bytes(Body)
        self.modified[(Bucket, Key)] = datetime(2026, 8, 28, tzinfo=UTC)

    def get_object(self, *, Bucket: str, Key: str):
        self.calls.append(("get", Bucket, Key))
        if self.failure:
            raise self.failure
        try:
            return {"Body": _Body(self.objects[(Bucket, Key)])}
        except KeyError:
            raise _ClientFailure(404, "NoSuchKey") from None

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.calls.append(("delete", Bucket, Key))
        if self.failure:
            raise self.failure
        self.objects.pop((Bucket, Key), None)
        self.modified.pop((Bucket, Key), None)

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        ContinuationToken: str | None = None,
    ):
        self.calls.append(("list", Bucket, ContinuationToken))
        if self.failure:
            raise self.failure
        start = int(ContinuationToken or 0)
        keys = sorted(
            key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)
        )
        selected = keys[start : start + self.page_size]
        next_start = start + len(selected)
        truncated = next_start < len(keys)
        response = {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[(Bucket, key)]),
                    "LastModified": self.modified[(Bucket, key)],
                }
                for key in selected
            ],
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextContinuationToken"] = str(next_start)
        return response

    def head_bucket(self, *, Bucket: str):
        self.calls.append(("head", Bucket, None))
        if self.failure:
            raise self.failure
        return {"BucketRegion": "test-region-1"}

    def get_bucket_location(self, *, Bucket: str):
        self.calls.append(("location", Bucket, None))
        return {"LocationConstraint": "test-region-1"}


class S3AdapterTests(unittest.TestCase):
    def test_IT_S3_001_config_reads_only_bucket_and_redacts_it(self):
        config = S3Config.from_env({"S3_BUCKET": "private-bucket"})

        self.assertEqual("private-bucket", config.bucket)
        self.assertEqual("S3Config(<redacted>)", repr(config))
        with self.assertRaises(S3ConfigurationError):
            S3Config.from_env({})

    def test_IT_S3_002_store_read_delete_and_size_contract(self):
        client = _Client()
        storage = S3Storage("private-bucket", client)

        location = storage.put("documents/key", b"data", 4)
        self.assertEqual("s3", location.provider)
        self.assertEqual(b"data", storage.get(location))
        storage.delete(location)
        storage.delete(location)

        with self.assertRaises(StorageUnavailable):
            storage.put("documents/bad", b"x", 2)
        client.objects[(storage.namespace, "documents/key")] = b"x"
        with self.assertRaises(StorageUnavailable):
            storage.get(location)

    def test_IT_S3_003_missing_and_other_sdk_failures_are_distinct(self):
        client = _Client()
        storage = S3Storage("private-bucket", client)
        location = StorageLocation("s3", storage.namespace, "missing", 1)

        with self.assertRaises(StorageObjectNotFound):
            storage.get(location)
        client.failure = _ClientFailure(503, "SlowDown")
        with self.assertRaises(StorageUnavailable):
            storage.get(location)

    def test_IT_S3_004_provider_and_bucket_mismatch_make_no_sdk_call(self):
        client = _Client()
        storage = S3Storage("private-bucket", client)

        for location in (
            StorageLocation("local", storage.namespace, "key", 1),
            StorageLocation("s3", "other-bucket", "key", 1),
        ):
            with self.subTest(location=location), self.assertRaises(
                StorageLocationMismatch
            ):
                storage.get(location)
        self.assertEqual([], client.calls)

    def test_IT_S3_005_preflight_rejects_endpoint_access_and_region_drift(self):
        client = _Client()
        storage = S3Storage("private-bucket", client)
        verify_s3(storage)

        client.meta.endpoint_url = "http://localhost:9000"
        with self.assertRaises(S3CompatibilityError):
            verify_s3(storage)
        client.meta.endpoint_url = "https://s3.test.amazonaws.com"
        client.meta.region_name = "other-region"
        with self.assertRaises(S3CompatibilityError):
            verify_s3(storage)
        client.meta.region_name = "test-region-1"
        client.failure = RuntimeError("secret endpoint detail")
        with self.assertRaisesRegex(S3CompatibilityError, "preflight failed"):
            verify_s3(storage)

    def test_IT_S3_006_builder_uses_shared_contract_without_storage_import(self):
        client = _Client()
        storage = build_s3_storage(
            S3Config("private-bucket"),
            client=client,
        )

        self.assertIs(client, storage.client)
        self.assertEqual("s3", storage.provider)

    def test_IT_S3_007_inventory_follows_pages_and_translates_failures(self):
        client = _Client()
        client.page_size = 1
        storage = S3Storage("private-bucket", client)
        storage.put("documents/a", b"a", 1)
        storage.put("documents/b", b"bb", 2)

        listed = tuple(storage.iter_objects("documents/"))

        self.assertEqual(["documents/a", "documents/b"], [row.location.key for row in listed])
        self.assertEqual([1, 2], [row.location.size for row in listed])
        self.assertEqual(2, sum(call[0] == "list" for call in client.calls))
        client.failure = RuntimeError("secret SDK detail")
        with self.assertRaises(StorageUnavailable):
            tuple(storage.iter_objects("documents/"))


if __name__ == "__main__":
    unittest.main()
