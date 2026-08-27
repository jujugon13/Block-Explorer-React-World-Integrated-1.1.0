from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.shared import (
    ObjectClientError,
    ObjectMissingError,
    StorageLocation,
    StorageLocationMismatch,
    StorageObjectNotFound,
    StorageUnavailable,
)
from src.storage import select_storage, store_or_reuse
from src.storage.local import LocalStorage
from src.storage.minio import MinioStorage
from src.storage.s3 import S3Storage


class FakeObjectClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.get_error: Exception | None = None

    def ensure_namespace(self, namespace: str) -> None:
        self.calls.append(("ensure", namespace, None))

    def put(self, namespace: str, key: str, data: bytes) -> None:
        self.calls.append(("put", namespace, key))
        self.objects[(namespace, key)] = bytes(data)

    def get(self, namespace: str, key: str) -> bytes:
        self.calls.append(("get", namespace, key))
        if self.get_error is not None:
            raise self.get_error
        try:
            return self.objects[(namespace, key)]
        except KeyError:
            raise ObjectMissingError from None

    def delete(self, namespace: str, key: str) -> None:
        self.calls.append(("delete", namespace, key))
        self.objects.pop((namespace, key), None)


class StorageAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def _cases(self):
        root = Path(self.temporary.name)
        minio_client = FakeObjectClient()
        s3_client = FakeObjectClient()
        return (
            ("local", LocalStorage(root / "local", "files"), None),
            ("minio", MinioStorage("files", minio_client), minio_client),
            ("s3", S3Storage("files", s3_client), s3_client),
        )

    @staticmethod
    def _object_count(storage, client) -> int:
        if client is not None:
            return len(client.objects)
        if not storage.root.exists():
            return 0
        return sum(path.is_file() for path in storage.root.rglob("*"))

    def test_AC_DOC_011_동일객체_재사용(self):
        data = b"same file"
        for name, storage, client in self._cases():
            with self.subTest(adapter=name):
                location, created = store_or_reuse(
                    storage,
                    existing=None,
                    key="documents/one/file.txt",
                    data=data,
                    expected_size=len(data),
                )
                before = self._object_count(storage, client)
                reused, created_again = store_or_reuse(
                    storage,
                    existing=location,
                    key="documents/two/other.txt",
                    data=data,
                    expected_size=len(data),
                )
                self.assertTrue(created)
                self.assertFalse(created_again)
                self.assertEqual(location, reused)
                self.assertEqual(before, self._object_count(storage, client))
                if client is not None:
                    self.assertEqual(1, sum(call[0] == "put" for call in client.calls))

    def test_AC_DOC_012_실패후_후보삭제(self):
        data = b"candidate"
        for name, storage, client in self._cases():
            with self.subTest(adapter=name):
                location, created = store_or_reuse(
                    storage,
                    existing=None,
                    key="documents/one/candidate.txt",
                    data=data,
                    expected_size=len(data),
                )
                try:
                    raise RuntimeError("transaction failed")
                except RuntimeError:
                    if created:
                        storage.delete(location)
                self.assertEqual(0, self._object_count(storage, client))

    def test_AC_DOC_013_저장소위치_불일치(self):
        for name, storage, client in self._cases():
            for field, location in (
                ("provider", StorageLocation("other", storage.namespace, "key", 1)),
                ("namespace", StorageLocation(storage.provider, "other", "key", 1)),
            ):
                with self.subTest(adapter=name, mismatch=field):
                    with self.assertRaises(StorageLocationMismatch) as raised:
                        store_or_reuse(
                            storage,
                            existing=location,
                            key="unused",
                            data=b"x",
                            expected_size=1,
                        )
                    self.assertEqual("DOCUMENT-STORAGE-003", raised.exception.code)
                    self.assertEqual(0, self._object_count(storage, client))
                    if client is not None:
                        self.assertEqual([], client.calls)

    def test_AC_DOC_037_없는객체_조회(self):
        for name, storage, client in self._cases():
            with self.subTest(adapter=name):
                location = StorageLocation(storage.provider, storage.namespace, "missing", 1)
                with self.assertRaises(StorageObjectNotFound) as raised:
                    storage.get(location)
                self.assertEqual("DOCUMENT-STORAGE-002", raised.exception.code)

        client = FakeObjectClient()
        client.get_error = ObjectClientError(status_code=404)
        with self.assertRaises(StorageObjectNotFound):
            S3Storage("files", client).get(StorageLocation("s3", "files", "missing", 1))

    def test_AC_DOC_038_조회크기_불일치(self):
        data = b"three"
        for name, storage, client in self._cases():
            with self.subTest(adapter=name):
                location = storage.put("documents/one/file.txt", data, len(data))
                if client is None:
                    (storage.root / location.key).write_bytes(b"x")
                else:
                    client.objects[(location.namespace, location.key)] = b"x"
                with self.assertRaises(StorageUnavailable) as raised:
                    storage.get(location)
                self.assertEqual("DOCUMENT-STORAGE-001", raised.exception.code)

    def test_AC_DOC_013_selector_defaults_and_external_validation(self):
        selected = select_storage({"storage.local.root": Path(self.temporary.name) / "default"})
        self.assertEqual("local", selected.provider)
        self.assertEqual("vectorshelf", selected.namespace)
        with self.assertRaises(ValueError):
            select_storage({"storage.type": "unknown"})
        with self.assertRaises(ValueError):
            select_storage({"storage.type": "minio"}, minio_client=FakeObjectClient())
        with self.assertRaises(ValueError):
            select_storage({"storage.type": "s3"}, s3_client=FakeObjectClient())

    def test_AC_DOC_012_minio_ensures_namespace_and_s3_does_not(self):
        minio_client = FakeObjectClient()
        MinioStorage("files", minio_client).put("key", b"x", 1)
        self.assertEqual(["ensure", "put"], [call[0] for call in minio_client.calls])

        s3_client = FakeObjectClient()
        S3Storage("files", s3_client).put("key", b"x", 1)
        self.assertEqual(["put"], [call[0] for call in s3_client.calls])

    def test_AC_DOC_012_local_uses_atomic_replace_and_removes_temporary_file(self):
        root = Path(self.temporary.name) / "atomic"
        storage = LocalStorage(root, "files")
        with patch("src.storage.local.adapter.os.replace", wraps=os.replace) as replace:
            location = storage.put("documents/id/file.txt", b"content", 7)
        replace.assert_called_once()
        self.assertEqual(b"content", storage.get(location))
        self.assertEqual([], list(root.rglob("*.tmp")))

    def test_AC_DOC_037_local_missing_delete_is_successful(self):
        storage = LocalStorage(Path(self.temporary.name) / "missing", "files")
        storage.delete(StorageLocation("local", "files", "not-there", 0))

    def test_AC_DOC_038_put_length_mismatch_writes_nothing(self):
        for name, storage, client in self._cases():
            with self.subTest(adapter=name):
                with self.assertRaises(StorageUnavailable):
                    storage.put("key", b"x", 2)
                self.assertEqual(0, self._object_count(storage, client))
                if client is not None:
                    self.assertEqual([], client.calls)


if __name__ == "__main__":
    unittest.main()
