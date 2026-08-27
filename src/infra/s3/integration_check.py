"""Destructive-on-temporary-keys AWS S3 stage-12 verification."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from src.documents import DocumentWorkspace, UploadFile
from src.shared import (
    Principal,
    StorageLocation,
    StorageLocationMismatch,
    StorageObjectNotFound,
    StorageUnavailable,
)

from .adapter import S3Storage, build_s3_storage


CHECKS = (
    "PREFLIGHT",
    "STORE",
    "READ",
    "DELETE_IDEMPOTENT",
    "CANDIDATE_CLEANUP",
    "NOT_FOUND_404",
    "SIZE_VALIDATION",
    "LOCATION_MISMATCH",
    "FINAL_CLEANUP",
)


def _expect(error_type: type[Exception], action: Callable[[], object]) -> None:
    try:
        action()
    except error_type:
        return
    raise AssertionError(f"expected {error_type.__name__}")


class _RecordingStorage:
    def __init__(self, storage: S3Storage, created: list[StorageLocation]) -> None:
        self.storage = storage
        self.provider = storage.provider
        self.namespace = storage.namespace
        self.created = created
        self.last_location: StorageLocation | None = None

    def ensure_location(self, location: StorageLocation) -> None:
        self.storage.ensure_location(location)

    def put(self, key: str, data: bytes, expected_size: int) -> StorageLocation:
        location = self.storage.put(key, data, expected_size)
        self.created.append(location)
        self.last_location = location
        return location

    def get(self, location: StorageLocation) -> bytes:
        return self.storage.get(location)

    def delete(self, location: StorageLocation) -> None:
        self.storage.delete(location)


def _candidate_cleanup(storage: S3Storage, created: list[StorageLocation]) -> None:
    recording = _RecordingStorage(storage, created)
    workspace = DocumentWorkspace(recording)
    workspace.fail_next_commit = True
    try:
        workspace.upload(
            Principal("stage12", user_id=1),
            UploadFile(b"candidate", "candidate.txt", "text/plain"),
            title="stage12",
            description=None,
            visibility="PRIVATE",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("candidate transaction unexpectedly committed")
    if recording.last_location is None:
        raise AssertionError("candidate object was not created")
    _expect(StorageObjectNotFound, lambda: storage.get(recording.last_location))


def main() -> int:
    status = {name: "UNVERIFIED" for name in CHECKS}
    storage: S3Storage | None = None
    created: list[StorageLocation] = []
    prefix = f"documents/{uuid4()}"

    try:
        storage = build_s3_storage()
        status["PREFLIGHT"] = "PASS"

        data = b"vectorshelf-stage12"
        location = storage.put(f"{prefix}/{uuid4()}.bin", data, len(data))
        created.append(location)
        status["STORE"] = "PASS"

        if storage.get(location) != data:
            raise AssertionError("stored bytes changed")
        status["READ"] = "PASS"

        deleted = storage.put(f"{prefix}/{uuid4()}.bin", b"delete", 6)
        created.append(deleted)
        storage.delete(deleted)
        storage.delete(deleted)
        status["DELETE_IDEMPOTENT"] = "PASS"

        _candidate_cleanup(storage, created)
        status["CANDIDATE_CLEANUP"] = "PASS"

        missing = StorageLocation(
            storage.provider,
            storage.namespace,
            f"{prefix}/{uuid4()}.missing",
            1,
        )
        _expect(StorageObjectNotFound, lambda: storage.get(missing))
        status["NOT_FOUND_404"] = "PASS"

        mismatch_key = f"{prefix}/{uuid4()}.bin"
        _expect(StorageUnavailable, lambda: storage.put(mismatch_key, b"x", 2))
        _expect(
            StorageObjectNotFound,
            lambda: storage.get(
                StorageLocation(storage.provider, storage.namespace, mismatch_key, 1)
            ),
        )
        sized = storage.put(f"{prefix}/{uuid4()}.bin", b"size", 4)
        created.append(sized)
        _expect(
            StorageUnavailable,
            lambda: storage.get(
                StorageLocation(sized.provider, sized.namespace, sized.key, 5)
            ),
        )
        status["SIZE_VALIDATION"] = "PASS"

        _expect(
            StorageLocationMismatch,
            lambda: storage.get(
                StorageLocation("local", storage.namespace, location.key, location.size)
            ),
        )
        _expect(
            StorageLocationMismatch,
            lambda: storage.get(
                StorageLocation(
                    storage.provider,
                    storage.namespace + "-mismatch",
                    location.key,
                    location.size,
                )
            ),
        )
        status["LOCATION_MISMATCH"] = "PASS"
    except Exception as error:
        current = next(name for name in CHECKS if status[name] == "UNVERIFIED")
        status[current] = f"FAIL:{type(error).__name__}"
    finally:
        if storage is not None:
            try:
                for location in created:
                    storage.delete(location)
                status["FINAL_CLEANUP"] = "PASS"
            except Exception as error:
                status["FINAL_CLEANUP"] = f"FAIL:{type(error).__name__}"

    for name in CHECKS:
        print(f"{name}={status[name]}")
    return 0 if all(value == "PASS" for value in status.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
