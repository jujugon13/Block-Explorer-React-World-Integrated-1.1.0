"""Active storage selection and candidate reuse."""

from __future__ import annotations

from os import PathLike
from typing import Mapping

from src.shared import ObjectClient, ObjectStorage, StorageLocation


def select_storage(
    settings: Mapping[str, object] | None = None,
    *,
    minio_client: ObjectClient | None = None,
    s3_client: ObjectClient | None = None,
) -> ObjectStorage:
    values = settings or {}
    provider = values.get("storage.type", "local")
    bucket = values.get("storage.bucket", "")

    if provider == "local":
        from .local import LocalStorage

        namespace = bucket if isinstance(bucket, str) and bucket else "vectorshelf"
        root = values.get("storage.local.root", "./data/vectorshelf")
        if not isinstance(root, (str, PathLike)):
            raise ValueError("storage.local.root must be a path")
        return LocalStorage(root, namespace)

    if provider not in {"minio", "s3"}:
        raise ValueError("storage.type must be local, minio, or s3")
    if not isinstance(bucket, str) or not bucket:
        raise ValueError("external storage requires storage.bucket")

    if provider == "minio":
        if minio_client is None:
            raise ValueError("minio client is required")
        from .minio import MinioStorage

        return MinioStorage(bucket, minio_client)

    if s3_client is None:
        raise ValueError("s3 client is required")
    from .s3 import S3Storage

    return S3Storage(bucket, s3_client)


def store_or_reuse(
    storage: ObjectStorage,
    *,
    existing: StorageLocation | None,
    key: str,
    data: bytes,
    expected_size: int,
) -> tuple[StorageLocation, bool]:
    if existing is not None:
        storage.ensure_location(existing)
        return existing, False
    return storage.put(key, data, expected_size), True


__all__ = ["select_storage", "store_or_reuse"]
