"""Real product-upload, S3, RDS, and forced-kill verification for AC-SYNC-010."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Event
from uuid import NAMESPACE_URL, uuid4, uuid5

from src.application_postgres import build_postgres_domain_components, build_postgres_ledger_adapters
from src.documents import UploadFile
from src.shared import Principal, StorageLocation, StorageObjectNotFound
from src.sync import SyncService
from src.sync.consistency import OrphanedDataDetector

from .adapter import build_s3_storage


_CHILD_FLAG = "--upload-child"
_RUN_ENV = "STAGE16_RUN_ID"
_REQUIRED_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "S3_BUCKET")


class _RunIds:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.count = 0
        self.values: set[str] = set()

    def __call__(self):
        self.count += 1
        value = uuid5(NAMESPACE_URL, f"stage16:{self.run_id}:{self.count}")
        self.values.add(str(value))
        return value


class _CrashAfterPutStorage:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.provider = storage.provider
        self.namespace = storage.namespace

    def ensure_location(self, location) -> None:
        self.storage.ensure_location(location)

    def put(self, key: str, data: bytes, expected_size: int):
        location = self.storage.put(key, data, expected_size)
        print(
            json.dumps(
                {
                    "checkpoint": "S3_PUT_RETURNED",
                    "provider": location.provider,
                    "namespace": location.namespace,
                    "key": location.key,
                    "size": location.size,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        Event().wait()
        return location

    def get(self, location):
        return self.storage.get(location)

    def delete(self, location) -> None:
        self.storage.delete(location)


class _FailList:
    def __init__(self, storage) -> None:
        self.storage = storage

    def iter_objects(self, prefix):
        del prefix
        raise ConnectionError("inventory unavailable")

    def delete(self, location) -> None:
        self.storage.delete(location)


class _FailDelete:
    def __init__(self, storage) -> None:
        self.storage = storage

    def iter_objects(self, prefix):
        return self.storage.iter_objects(prefix)

    def delete(self, location) -> None:
        del location
        raise ConnectionError("delete unavailable")


class _FailReferences:
    def is_referenced(self, location) -> bool:
        del location
        raise ConnectionError("reference lookup unavailable")


class _CountingReferences:
    def __init__(self, references) -> None:
        self.references = references
        self.calls = 0

    def is_referenced(self, location) -> bool:
        self.calls += 1
        return self.references.is_referenced(location)


def _prefix(location: StorageLocation) -> str:
    return location.key.rsplit("/", 1)[0] + "/"


def _force_kill(process: subprocess.Popen[str]) -> bool:
    command = (
        ["taskkill", "/PID", str(process.pid), "/T", "/F"]
        if os.name == "nt"
        else ["kill", "-9", str(process.pid)]
    )
    killed = subprocess.run(command, capture_output=True)
    process.wait(timeout=10)
    return killed.returncode == 0


def _child() -> int:
    run_id = os.environ[_RUN_ENV]
    storage = build_s3_storage()
    adapters = build_postgres_ledger_adapters()
    domain = build_postgres_domain_components(_CrashAfterPutStorage(storage), adapters)
    domain.documents.upload(
        Principal(f"stage16-{run_id}@example.invalid", user_id=1),
        UploadFile(f"candidate:{run_id}".encode(), f"{run_id}.txt", "text/plain"),
        title=f"stage16-{run_id}-candidate",
        description=None,
        visibility="PRIVATE",
    )
    return 0


def _service(adapters, inventory, references, location, future, ids) -> SyncService:
    return SyncService(
        adapters.sync_store,
        detector=OrphanedDataDetector(
            inventory,
            references,
            clock=lambda: future,
            grace=timedelta(hours=24),
            prefix=_prefix(location),
        ),
        clock=lambda: future,
        uuid_factory=ids,
    )


def _expect_preserved(service: SyncService, storage, location, *, mode: str) -> str:
    try:
        service.reconcile(mode=mode, actor_id="stage16")
    except Exception as error:
        storage.get(location)
        return type(error).__name__
    raise AssertionError("injected reconciliation failure did not fail")


def _cleanup(
    adapters,
    document_id: int | None,
    issue_ids: set[str],
    generated_ids: set[str],
    run_id: str | None,
) -> None:
    with adapters.transactions.operation() as connection:
        cursor = connection.cursor()
        try:
            if generated_ids:
                values = list(generated_ids)
                cursor.execute(
                    "DELETE FROM operator_actions "
                    "WHERE action_id = ANY(%s::uuid[]) OR target_id = ANY(%s)",
                    (values, values),
                )
                cursor.execute(
                    "DELETE FROM reconciliation_runs "
                    "WHERE reconciliation_id = ANY(%s::uuid[])",
                    (values,),
                )
            if issue_ids:
                values = list(issue_ids)
                cursor.execute(
                    "DELETE FROM operator_actions WHERE target_id = ANY(%s)",
                    (values,),
                )
                cursor.execute(
                    "DELETE FROM consistency_issues WHERE issue_id = ANY(%s::uuid[])",
                    (values,),
                )
            if document_id is not None:
                cursor.execute(
                    "SELECT document_version_id, file_object_id "
                    "FROM document_versions WHERE document_id=%s",
                    (document_id,),
                )
                versions = tuple(cursor.fetchall())
                version_ids = [int(row[0]) for row in versions]
                file_ids = [int(row[1]) for row in versions]
                cursor.execute(
                    "SELECT event_id FROM sync_events "
                    "WHERE payload->>'documentId'=%s",
                    (str(document_id),),
                )
                event_ids = [str(row[0]) for row in cursor.fetchall()]
                if event_ids:
                    cursor.execute(
                        "DELETE FROM sync_delivery_attempts "
                        "WHERE event_id = ANY(%s::uuid[])",
                        (event_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM sync_events WHERE event_id = ANY(%s::uuid[])",
                        (event_ids,),
                    )
                if version_ids:
                    params = (version_ids,)
                    cursor.execute(
                        "DELETE FROM indexing_events WHERE job_id IN "
                        "(SELECT job_id FROM indexing_jobs "
                        "WHERE document_version_id=ANY(%s))",
                        params,
                    )
                    cursor.execute(
                        "DELETE FROM indexing_attempts WHERE job_id IN "
                        "(SELECT job_id FROM indexing_jobs "
                        "WHERE document_version_id=ANY(%s))",
                        params,
                    )
                    cursor.execute(
                        "DELETE FROM document_vectors "
                        "WHERE document_version_id=ANY(%s)",
                        params,
                    )
                    cursor.execute(
                        "DELETE FROM document_chunks "
                        "WHERE document_version_id=ANY(%s)",
                        params,
                    )
                    cursor.execute(
                        "DELETE FROM indexing_jobs "
                        "WHERE document_version_id=ANY(%s)",
                        params,
                    )
                    cursor.execute(
                        "DELETE FROM document_versions "
                        "WHERE document_version_id=ANY(%s)",
                        params,
                    )
                cursor.execute(
                    "DELETE FROM document_permission_cache WHERE document_id=%s",
                    (document_id,),
                )
                cursor.execute(
                    "DELETE FROM collection_documents WHERE document_id=%s",
                    (document_id,),
                )
                cursor.execute(
                    "DELETE FROM direct_permissions WHERE document_id=%s",
                    (document_id,),
                )
                cursor.execute("DELETE FROM documents WHERE document_id=%s", (document_id,))
                if file_ids:
                    cursor.execute(
                        "DELETE FROM file_objects WHERE file_object_id=ANY(%s)",
                        (file_ids,),
                    )
            if run_id is not None:
                email = f"stage16-{run_id}@example.invalid"
                cursor.execute(
                    "DELETE FROM user_roles WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE email=%s)",
                    (email,),
                )
                cursor.execute("DELETE FROM users WHERE email=%s", (email,))
                cursor.execute(
                    "DELETE FROM departments WHERE department_id=%s AND name=%s",
                    (int(run_id[:10], 16), f"stage16-{run_id}"),
                )
        finally:
            cursor.close()


def main() -> int:
    if _CHILD_FLAG in sys.argv:
        return _child()
    absent = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if absent:
        print("ITEM3_EXTERNAL_EFFECT=BLOCKED missing=" + ",".join(absent))
        return 2

    process: subprocess.Popen[str] | None = None
    adapters = storage = candidate = committed = None
    run_id = os.environ.get(_RUN_ENV) or uuid4().hex
    document_id: int | None = None
    issue_ids: set[str] = set()
    generated_ids: set[str] = set()
    ids = _RunIds(run_id)
    try:
        adapters = build_postgres_ledger_adapters()
        if adapters.capabilities is None:
            raise AssertionError("PostgreSQL preflight was not executed")
        print("POSTGRES_PREFLIGHT=PASS")
        storage = build_s3_storage()
        print("S3_PREFLIGHT=PASS")

        environment = dict(os.environ)
        environment[_RUN_ENV] = run_id
        process = subprocess.Popen(
            [sys.executable, "-m", "src.infra.s3.orphan_recovery_check", _CHILD_FLAG],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        if process.stdout is None:
            raise AssertionError("upload child stdout is unavailable")
        checkpoint = json.loads(process.stdout.readline())
        if checkpoint.get("checkpoint") != "S3_PUT_RETURNED":
            raise AssertionError("product upload did not reach the S3 checkpoint")
        candidate = StorageLocation(
            str(checkpoint["provider"]),
            str(checkpoint["namespace"]),
            str(checkpoint["key"]),
            int(checkpoint["size"]),
        )
        if not _force_kill(process):
            raise AssertionError("forced process termination failed")
        print("ITEM3_FORCE_KILL=PASS")

        domain = build_postgres_domain_components(storage, adapters)
        department_id = int(run_id[:10], 16)
        domain.users.seed_department(department_id, f"stage16-{run_id}")
        domain.users.seed_role("USER")
        user = domain.users.create_user(
            f"stage16-{run_id}@example.invalid",
            "stage16-not-a-login-secret",
            f"stage16-{run_id}",
            department_id,
            datetime.now(UTC),
        )
        principal = Principal(
            user.email,
            frozenset({"USER"}),
            user.id,
            department_id,
            user.name,
        )
        committed_data = f"committed:{run_id}".encode()
        uploaded = domain.documents.upload(
            principal,
            UploadFile(
                committed_data,
                f"{run_id}-committed.txt",
                "text/plain",
            ),
            title=f"stage16-{run_id}-committed",
            description=None,
            visibility="PRIVATE",
        )
        document_id = int(uploaded.data["documentId"])
        file_row = adapters.document_store.find_file(
            sha256(committed_data).hexdigest(), len(committed_data)
        )
        if file_row is None:
            raise AssertionError("product upload did not commit the file ledger")
        committed = file_row.location
        future = datetime.now(UTC) + timedelta(hours=25)

        errors = {
            "list": _expect_preserved(
                _service(
                    adapters,
                    _FailList(storage),
                    adapters.document_store,
                    candidate,
                    future,
                    ids,
                ),
                storage,
                candidate,
                mode="DRY_RUN",
            ),
            "db": _expect_preserved(
                _service(
                    adapters,
                    storage,
                    _FailReferences(),
                    candidate,
                    future,
                    ids,
                ),
                storage,
                candidate,
                mode="REPAIR",
            ),
            "delete": _expect_preserved(
                _service(
                    adapters,
                    _FailDelete(storage),
                    adapters.document_store,
                    candidate,
                    future,
                    ids,
                ),
                storage,
                candidate,
                mode="REPAIR",
            ),
        }
        if set(errors) != {"list", "db", "delete"}:
            raise AssertionError("failure preservation checks were incomplete")
        print("ITEM3_FAILURES_PRESERVE_OBJECT=PASS")

        before = {issue.id for issue in domain.sync.issues(issue_type="ORPHANED_DATA")}
        dry = _service(
            adapters,
            storage,
            adapters.document_store,
            candidate,
            future,
            ids,
        )
        dry.reconcile(mode="DRY_RUN", actor_id="stage16")
        issue_ids = {
            issue.id for issue in dry.issues(issue_type="ORPHANED_DATA")
        } - before
        if len(issue_ids) != 1 or dry.issue(next(iter(issue_ids))).status != "OPEN":
            raise AssertionError("DRY_RUN did not report exactly one new orphan")
        storage.get(candidate)
        print("ITEM3_DRY_RUN_ORPHANED_DATA=PASS")

        referenced = _service(
            adapters,
            storage,
            adapters.document_store,
            committed,
            future,
            ids,
        )
        referenced.reconcile(mode="REPAIR", actor_id="stage16")
        if storage.get(committed) != committed_data:
            raise AssertionError("referenced object changed")
        print("ITEM3_REFERENCED_OBJECT_PRESERVED=PASS")

        counted = _CountingReferences(adapters.document_store)
        repair = _service(adapters, storage, counted, candidate, future, ids)
        repair.reconcile(mode="REPAIR", actor_id="stage16")
        if counted.calls != 2:
            raise AssertionError("REPAIR did not recheck the reference before delete")
        try:
            storage.get(candidate)
        except StorageObjectNotFound:
            pass
        else:
            raise AssertionError("orphan candidate still exists after REPAIR")
        if repair.issue(next(iter(issue_ids))).status != "RESOLVED":
            raise AssertionError("repaired orphan issue was not resolved")
        print("ITEM3_REPAIR_ORPHAN_REMOVED=PASS")
        print("ITEM3_EXTERNAL_EFFECT=PASS")
        return 0
    except Exception as error:
        print(f"ITEM3_EXTERNAL_EFFECT=FAIL errorClass={type(error).__name__}")
        return 1
    finally:
        if process is not None and process.poll() is None:
            try:
                _force_kill(process)
            except Exception:
                pass
        generated_ids.update(ids.values)
        if adapters is not None:
            try:
                _cleanup(adapters, document_id, issue_ids, generated_ids, run_id)
            except Exception:
                pass
        if storage is not None:
            for location in (candidate, committed):
                if location is not None:
                    try:
                        storage.delete(location)
                    except Exception:
                        pass


if __name__ == "__main__":
    raise SystemExit(main())
