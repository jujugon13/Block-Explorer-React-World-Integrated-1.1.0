#!/usr/bin/env python3
"""Destructive stage-16 checks against an explicitly authorized test RDS/S3."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.stage16_outage import (
    Blocked,
    interrupt_and_observe,
    multi_az_target,
    probe,
    single_az_target,
)


SCENARIOS = ("16-1", "16-2", "16-3", "16-4a", "16-4b", "16-5", "16-6")
DB_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "S3_BUCKET")


def emit(event: str, **values: object) -> None:
    print(json.dumps({"event": event, **values}, default=str, sort_keys=True), flush=True)


def marker_write(path: str, **values: object) -> None:
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(values, stream, default=str, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def marker_wait(path: Path, timeout: float = 60.0) -> dict[str, object]:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        Event().wait(0.05)
    raise TimeoutError(f"checkpoint not reached: {path.name}")


def observer(sql: str, parameters: tuple[object, ...] = ()) -> tuple[tuple[object, ...], ...]:
    from src.infra.postgres.config import PostgresConfig, connect

    connection = connect(PostgresConfig.from_env())
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, parameters)
            return tuple(tuple(row) for row in cursor.fetchall())
        finally:
            cursor.close()
    finally:
        connection.rollback()
        connection.close()


def stack(*, lease_seconds: float = 2.0, clock=None):
    from src.application_postgres import (
        build_postgres_domain_components,
        build_postgres_ledger_adapters,
    )
    from src.infra.s3 import build_s3_storage

    storage = build_s3_storage()
    adapters = build_postgres_ledger_adapters(apply_migrations=False)
    domain = build_postgres_domain_components(
        storage,
        adapters,
        clock=clock,
        indexing_options={
            "lease_duration": timedelta(seconds=lease_seconds),
            "dead_threshold": timedelta(seconds=lease_seconds),
            "retry_initial": timedelta(seconds=1),
            "retry_jitter": 0,
        },
        sync_options={
            "dispatcher_enabled": True,
            "poll_interval": timedelta(milliseconds=100),
            "lease_duration": timedelta(seconds=lease_seconds),
            "recovery_interval": timedelta(seconds=1),
            "retry_initial": timedelta(seconds=1),
        },
    )
    return storage, adapters, domain


def ensure_user(domain, run_id: str):
    from src.shared import Principal

    department_id = int(run_id[:10], 16)
    email = f"stage16-{run_id}@example.invalid"
    domain.users.seed_department(department_id, f"stage16-{run_id}")
    domain.users.seed_role("USER")
    user = domain.users.find_by_email(email)
    if user is None:
        user = domain.users.create_user(
            email,
            "stage16-not-a-login-secret",
            f"stage16-{run_id}",
            department_id,
            datetime.now(UTC),
        )
    return Principal(email, frozenset({"USER"}), user.id, department_id, user.name)


def upload(domain, principal, run_id: str, suffix: str):
    from src.documents import UploadFile

    data = f"stage16:{run_id}:{suffix}".encode()
    return domain.documents.upload(
        principal,
        UploadFile(data, f"{run_id}-{suffix}.txt", "text/plain"),
        title=f"stage16-{run_id}-{suffix}",
        description=None,
        visibility="PRIVATE",
    )


def force_kill(process: subprocess.Popen[object]) -> bool:
    command = (
        ["taskkill", "/PID", str(process.pid), "/T", "/F"]
        if os.name == "nt"
        else ["kill", "-9", str(process.pid)]
    )
    result = subprocess.run(command, capture_output=True)
    process.wait(timeout=10)
    return result.returncode == 0


def child_process(*arguments: str) -> subprocess.Popen[object]:
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def no_unrelated_claimable_job(job_id: int) -> None:
    rows = observer(
        "SELECT count(*) FROM indexing_jobs "
        "WHERE status='PENDING' AND (next_run_at IS NULL OR next_run_at<=now()) "
        "AND job_id<>%s",
        (job_id,),
    )
    if int(rows[0][0]) != 0:
        raise Blocked("unrelated claimable indexing jobs exist in the test database")


def no_unrelated_pending_event(event_id: str) -> None:
    rows = observer(
        "SELECT count(*) FROM sync_events "
        "WHERE status='PENDING' AND available_at<=now() AND event_id<>%s",
        (event_id,),
    )
    if int(rows[0][0]) != 0:
        raise Blocked("unrelated claimable outbox events exist in the test database")


def event_for_document(document_id: int) -> str:
    rows = observer(
        "SELECT event_id::text FROM sync_events "
        "WHERE payload->>'documentId'=%s ORDER BY occurred_at,event_id",
        (str(document_id),),
    )
    if len(rows) != 1:
        raise AssertionError("test document did not create exactly one outbox event")
    return str(rows[0][0])


def child_claim(args: argparse.Namespace, *, wait: bool) -> int:
    _, _, domain = stack()
    worker = domain.indexing.register_worker(
        args.instance_id,
        name=f"stage16-{args.run_id}",
        hostname=f"pid-{os.getpid()}",
        now=datetime.now(UTC),
    )
    if args.barrier:
        marker_write(args.ready, registered=True)
        marker_wait(Path(args.barrier))
    claim = domain.indexing.claim(worker.id, datetime.now(UTC))
    data = claim.data or {}
    values = {
        "workerId": worker.id,
        "status": claim.status,
        "jobId": data.get("jobId"),
        "claimToken": data.get("claimToken"),
    }
    if wait and claim.status == 200:
        attempt = domain.indexing.start_attempt(
            int(data["jobId"]), worker.id, str(data["claimToken"])
        )
        values["attemptId"] = attempt.data["attemptId"]
    marker_write(args.result or args.ready, **values)
    if wait:
        Event().wait()
    return 0


class CommitBarrierConnection:
    def __init__(self, connection, phase: str, marker: str, title: str, control: dict[str, bool]):
        self.connection = connection
        self.phase = phase
        self.marker = marker
        self.title = title
        self.control = control

    def __getattr__(self, name: str):
        return getattr(self.connection, name)

    def commit(self) -> None:
        if not self.control["armed"]:
            self.connection.commit()
            return
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                "SELECT document_id,latest_version_id FROM documents WHERE title=%s",
                (self.title,),
            )
            document = cursor.fetchone()
            if document is None:
                self.connection.commit()
                return
            document_id = int(document[0])
            version_id = int(document[1])
            cursor.execute(
                "SELECT event_id::text FROM sync_events WHERE payload->>'documentId'=%s",
                (None if document_id is None else str(document_id),),
            )
            event_ids = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT f.storage_provider,f.storage_namespace,f.storage_key,f.file_size "
                "FROM file_objects f JOIN document_versions v "
                "ON v.file_object_id=f.file_object_id WHERE v.document_id=%s",
                (document_id,),
            )
            location = cursor.fetchone()
        finally:
            cursor.close()
        checkpoint = {
            "documentId": document_id,
            "versionId": version_id,
            "eventIds": event_ids,
            "storage": None if location is None else tuple(location),
            "phase": self.phase,
        }
        if self.phase == "before":
            marker_write(self.marker, **checkpoint)
            Event().wait()
        self.connection.commit()
        marker_write(self.marker, **checkpoint)
        Event().wait()


def child_commit(args: argparse.Namespace) -> int:
    from functools import partial

    from src.application_postgres import (
        build_postgres_domain_components,
        build_postgres_ledger_adapters,
    )
    from src.infra.postgres.config import PostgresConfig, connect
    from src.infra.s3 import build_s3_storage
    from src.shared import Principal

    config = PostgresConfig.from_env()
    control = {"armed": False}

    def factory():
        return CommitBarrierConnection(
            connect(config), args.phase, args.ready, args.title, control
        )

    adapters = build_postgres_ledger_adapters(
        config=config,
        connection_factory=factory,
        apply_migrations=False,
        verify=False,
    )
    domain = build_postgres_domain_components(build_s3_storage(), adapters)
    principal = Principal(
        args.user_subject,
        frozenset({"USER"}),
        int(args.user_id),
        display_name=args.user_subject,
    )
    control["armed"] = True
    upload(domain, principal, args.run_id, args.phase)
    return 0


class CrashOutboxUnitOfWork:
    def __init__(self, delegate, event_id: str, marker: str) -> None:
        self.delegate = delegate
        self.event_id = event_id
        self.marker = marker

    def commit(self, event, mark_processed) -> None:
        if event.id != self.event_id:
            raise AssertionError("dispatcher claimed an unrelated event")

        def checkpoint() -> None:
            marker_write(self.marker, eventId=event.id)
            Event().wait()

        self.delegate.commit(event, checkpoint)


def child_dispatch(args: argparse.Namespace) -> int:
    from src.sync import SyncDispatcher

    _, _, domain = stack()
    dispatcher = SyncDispatcher(
        domain.sync,
        CrashOutboxUnitOfWork(domain.sync_handlers, args.event_id, args.ready),
    )
    dispatcher.tick()
    return 0


def scenario_16_1(run_id: str) -> dict[str, object]:
    _, _, domain = stack()
    result = upload(domain, ensure_user(domain, run_id), run_id, "claim")
    job_id = int(result.data["embeddingJobId"])
    no_unrelated_claimable_job(job_id)
    with tempfile.TemporaryDirectory(prefix="stage16-1-", dir=Path.cwd()) as directory:
        root = Path(directory)
        barrier = root / "go.json"
        children = [
            child_process(
                "--child", "claim", "--run-id", run_id,
                "--instance-id", f"{run_id}-node-{index}",
                "--ready", str(root / f"ready-{index}.json"),
                "--result", str(root / f"result-{index}.json"),
                "--barrier", str(barrier),
            )
            for index in range(2)
        ]
        try:
            for index in range(2):
                marker_wait(root / f"ready-{index}.json")
            marker_write(str(barrier), released=True)
            results = [marker_wait(root / f"result-{index}.json") for index in range(2)]
            for child in children:
                child.wait(timeout=30)
        finally:
            for child in children:
                if child.poll() is None:
                    force_kill(child)
    rows = observer(
        "SELECT status," 
        "(SELECT count(*) FROM indexing_events WHERE job_id=%s AND event_type='LOCKED'),"
        "(SELECT count(*) FROM indexing_attempts WHERE job_id=%s),"
        "(SELECT count(*) FROM document_chunks c JOIN indexing_jobs j "
        "ON j.document_version_id=c.document_version_id WHERE j.job_id=%s),"
        "(SELECT count(*) FROM document_vectors v JOIN indexing_jobs j "
        "ON j.document_version_id=v.document_version_id WHERE j.job_id=%s) "
        "FROM indexing_jobs WHERE job_id=%s",
        (job_id, job_id, job_id, job_id, job_id),
    )
    statuses = sorted(int(item["status"]) for item in results)
    claimed = [item.get("jobId") for item in results if int(item["status"]) == 200]
    passed = (
        statuses == [200, 204]
        and claimed == [job_id]
        and rows == (("PROCESSING", 1, 0, 0, 0),)
    )
    return {"status": "PASS" if passed else "FAIL", "jobId": job_id}


def _finish_job(domain, job_id: int, when: datetime, run_id: str):
    from src.infra.ai import EMBEDDING_DIMENSION

    worker = domain.indexing.register_worker(
        f"{run_id}-recovery-{uuid4().hex}", name="stage16-recovery", now=when
    )
    claim = domain.indexing.claim(worker.id, when)
    if claim.status != 200 or claim.data is None or int(claim.data["jobId"]) != job_id:
        raise AssertionError("recovery worker did not claim the exact test job")
    token = str(claim.data["claimToken"])
    attempt = domain.indexing.start_attempt(job_id, worker.id, token, when)
    attempt_id = int(attempt.data["attemptId"])
    domain.indexing.save_chunks(
        job_id,
        attempt_id,
        worker.id,
        token,
        chunks=(f"stage16 recovery {job_id}",),
        now=when,
    )
    domain.indexing.save_embeddings(
        job_id,
        attempt_id,
        worker.id,
        token,
        lambda records, model: ((0.0,) * EMBEDDING_DIMENSION for _ in records),
        now=when,
    )
    domain.indexing.complete(
        job_id,
        attempt_id,
        worker.id,
        token,
        when + timedelta(seconds=1),
    )
    return claim


def scenario_16_2(run_id: str) -> dict[str, object]:
    _, _, domain = stack()
    result = upload(domain, ensure_user(domain, run_id), run_id, "crash-claim")
    job_id = int(result.data["embeddingJobId"])
    no_unrelated_claimable_job(job_id)
    with tempfile.TemporaryDirectory(prefix="stage16-2-", dir=Path.cwd()) as directory:
        ready = Path(directory) / "claimed.json"
        child = child_process(
            "--child", "claim-wait", "--run-id", run_id,
            "--instance-id", f"{run_id}-crashed", "--ready", str(ready),
        )
        try:
            claimed = marker_wait(ready)
            if int(claimed.get("jobId", -1)) != job_id:
                raise AssertionError("crashed worker claimed an unrelated job")
            if not force_kill(child):
                raise AssertionError("forced worker termination failed")
        finally:
            if child.poll() is None:
                force_kill(child)
    recovery_at = datetime.now(UTC) + timedelta(seconds=3)
    summary = domain.indexing.recover_expired(recovery_at, batch_size=10)
    new_claim = _finish_job(
        domain, job_id, recovery_at + timedelta(seconds=2), run_id
    )
    rejected = 0
    for action in (
        lambda: domain.indexing.renew(
            job_id, int(claimed["workerId"]), str(claimed["claimToken"]), recovery_at
        ),
        lambda: domain.indexing.complete(
            job_id,
            int(claimed["attemptId"]),
            int(claimed["workerId"]),
            str(claimed["claimToken"]),
            recovery_at,
        ),
    ):
        try:
            action()
        except Exception:
            rejected += 1
    rows = observer(
        "SELECT j.status,"
        "(SELECT count(*) FROM indexing_events WHERE job_id=j.job_id AND event_type='LEASE_EXPIRED'),"
        "(SELECT count(*) FROM indexing_events WHERE job_id=j.job_id AND event_type='INDEXED'),"
        "(SELECT count(*) FROM document_chunks WHERE document_version_id=j.document_version_id),"
        "(SELECT count(*) FROM document_vectors WHERE document_version_id=j.document_version_id),"
        "(SELECT count(*) FROM indexing_attempts WHERE job_id=j.job_id AND status='SUCCESS') "
        "FROM indexing_jobs j WHERE j.job_id=%s",
        (job_id,),
    )
    passed = (
        summary["recovered"] == 1
        and rejected == 2
        and new_claim.data["claimToken"] != claimed["claimToken"]
        and rows == (("INDEXED", 1, 1, 1, 1, 1),)
    )
    return {"status": "PASS" if passed else "FAIL", "jobId": job_id}


def scenario_16_3(run_id: str) -> dict[str, object]:
    environment = dict(os.environ)
    environment["STAGE16_RUN_ID"] = run_id
    result = subprocess.run(
        [sys.executable, "-m", "src.infra.s3.orphan_recovery_check"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    passed = result.returncode == 0 and "ITEM3_EXTERNAL_EFFECT=PASS" in result.stdout
    return {"status": "PASS" if passed else "FAIL"}


def _commit_counts(title: str) -> tuple[int, int, int, int]:
    rows = observer(
        "SELECT "
        "(SELECT count(*) FROM documents WHERE title=%s),"
        "(SELECT count(*) FROM document_versions v JOIN documents d "
        "ON d.document_id=v.document_id WHERE d.title=%s),"
        "(SELECT count(*) FROM indexing_jobs j JOIN document_versions v "
        "ON v.document_version_id=j.document_version_id JOIN documents d "
        "ON d.document_id=v.document_id WHERE d.title=%s),"
        "(SELECT count(*) FROM sync_events e WHERE e.payload->>'documentId' IN "
        "(SELECT document_id::text FROM documents WHERE title=%s))",
        (title, title, title, title),
    )
    return tuple(int(value) for value in rows[0])  # type: ignore[return-value]


def scenario_16_4(run_id: str, phase: str) -> dict[str, object]:
    from src.shared import StorageLocation

    storage, _, domain = stack()
    principal = ensure_user(domain, run_id)
    title = f"stage16-{run_id}-{phase}"
    with tempfile.TemporaryDirectory(prefix=f"stage16-4{phase}-", dir=Path.cwd()) as directory:
        marker = Path(directory) / "commit.json"
        child = child_process(
            "--child", "commit", "--run-id", run_id, "--phase", phase,
            "--title", title, "--user-id", str(principal.user_id),
            "--user-subject", principal.subject, "--ready", str(marker),
        )
        try:
            checkpoint = marker_wait(marker)
            if not force_kill(child):
                raise AssertionError("forced commit process termination failed")
        finally:
            if child.poll() is None:
                force_kill(child)
    counts = _commit_counts(title)
    if phase == "before":
        location = checkpoint.get("storage")
        if not isinstance(location, list) or len(location) != 4:
            raise AssertionError("rollback checkpoint did not retain the S3 location")
        storage.delete(
            StorageLocation(
                str(location[0]).lower(), str(location[1]), str(location[2]), int(location[3])
            )
        )
        return {"status": "PASS" if counts == (0, 0, 0, 0) else "FAIL"}
    if counts != (1, 1, 1, 1):
        return {"status": "FAIL"}
    document_id = int(checkpoint["documentId"])
    event_id = event_for_document(document_id)
    no_unrelated_pending_event(event_id)
    with tempfile.TemporaryDirectory(prefix="stage16-4-dispatch-", dir=Path.cwd()) as directory:
        marker = Path(directory) / "dispatch.json"
        child = child_process(
            "--child", "dispatch-wait", "--run-id", run_id,
            "--event-id", event_id, "--ready", str(marker),
        )
        try:
            reached = marker_wait(marker)
            if reached.get("eventId") != event_id or not force_kill(child):
                raise AssertionError("dispatcher was not killed during the test event")
        finally:
            if child.poll() is None:
                force_kill(child)
    replay_at = datetime.now(UTC) + timedelta(seconds=3)
    clock = [replay_at]
    _, _, replay = stack(clock=lambda: clock[0])
    recovered = replay.sync.recover_expired(now=replay_at)
    clock[0] = replay_at + timedelta(seconds=2)
    dispatched = replay.sync_dispatcher.tick()
    rows = observer(
        "SELECT e.status,"
        "(SELECT count(*) FROM sync_delivery_attempts WHERE event_id=e.event_id),"
        "(SELECT count(*) FROM sync_delivery_attempts WHERE event_id=e.event_id AND status='FAILED'),"
        "(SELECT count(*) FROM sync_delivery_attempts WHERE event_id=e.event_id AND status='SUCCEEDED'),"
        "(SELECT count(*) FROM indexing_jobs WHERE document_version_id=%s) "
        "FROM sync_events e WHERE e.event_id=%s",
        (int(checkpoint["versionId"]), event_id),
    )
    passed = len(recovered) == 1 and dispatched is not None and rows == (("PROCESSED", 2, 1, 1, 1),)
    return {"status": "PASS" if passed else "FAIL"}


def _prepare_outage(domain, run_id: str) -> tuple[int, dict[str, object], str]:
    result = upload(domain, ensure_user(domain, run_id), run_id, "outage")
    job_id = int(result.data["embeddingJobId"])
    document_id = int(result.data["documentId"])
    event_id = event_for_document(document_id)
    no_unrelated_claimable_job(job_id)
    no_unrelated_pending_event(event_id)
    worker = domain.indexing.register_worker(
        f"{run_id}-outage-worker", name="stage16-outage", now=datetime.now(UTC)
    )
    claim = domain.indexing.claim(worker.id, datetime.now(UTC))
    if claim.status != 200 or claim.data is None or int(claim.data["jobId"]) != job_id:
        raise AssertionError("outage setup did not claim the exact test job")
    attempt = domain.indexing.start_attempt(
        job_id, worker.id, str(claim.data["claimToken"]), datetime.now(UTC)
    )
    claimed_event = domain.sync.claim(f"stage16-{run_id}", datetime.now(UTC))
    if claimed_event is None or claimed_event.id != event_id:
        raise AssertionError("outage setup did not claim the exact test event")
    values = dict(claim.data)
    values["attemptId"] = attempt.data["attemptId"]
    return job_id, values, event_id


def _recover_outage(
    domain,
    job_id: int,
    old_claim: dict[str, object],
    event_id: str,
    run_id: str,
) -> bool:
    recovery_at = datetime.now(UTC)
    recovered_jobs = domain.indexing.recover_expired(recovery_at, batch_size=10)
    _finish_job(domain, job_id, recovery_at + timedelta(seconds=2), run_id)
    recovered_events = domain.sync.recover_expired(now=recovery_at)
    Event().wait(1.1)
    replayed = domain.sync_dispatcher.tick()
    rows = observer(
        "SELECT j.status,e.status,"
        "(SELECT count(*) FROM document_chunks WHERE document_version_id=j.document_version_id),"
        "(SELECT count(*) FROM document_vectors WHERE document_version_id=j.document_version_id),"
        "(SELECT count(*) FROM indexing_events WHERE job_id=j.job_id AND event_type='INDEXED'),"
        "(SELECT count(*) FROM indexing_jobs WHERE document_version_id=j.document_version_id),"
        "(SELECT count(*) FROM sync_delivery_attempts WHERE event_id=e.event_id AND status='SUCCEEDED') "
        "FROM indexing_jobs j CROSS JOIN sync_events e WHERE j.job_id=%s AND e.event_id=%s",
        (job_id, event_id),
    )
    return (
        recovered_jobs["recovered"] == 1
        and len(recovered_events) == 1
        and replayed is not None
        and rows == (("INDEXED", "PROCESSED", 1, 1, 1, 1, 1),)
        and bool(old_claim.get("claimToken"))
    )


def scenario_outage(run_id: str, *, multi_az: bool) -> dict[str, object]:
    client, target, trigger = multi_az_target() if multi_az else single_az_target()
    if probe(run_id) != (True, True, True):
        raise AssertionError("pre-outage uvicorn API/search probe failed")
    _, _, domain = stack()
    job_id, claim, event_id = _prepare_outage(domain, run_id)
    observed = interrupt_and_observe(
        client, target, trigger, run_id, require_topology=multi_az
    )
    recovered = _recover_outage(domain, job_id, claim, event_id, run_id)
    topology = bool(observed["writerChanged"] or observed["primaryAzChanged"])
    passed = recovered and (topology if multi_az else True)
    return {
        "status": "PASS" if passed else "FAIL",
        "outageObserved": observed["outageObserved"],
        "recoverySeconds": observed["recoverySeconds"],
        "topologyChanged": topology,
    }


def cleanup_run(run_id: str) -> None:
    from src.application_postgres import build_postgres_ledger_adapters
    from src.infra.s3 import build_s3_storage
    from src.shared import StorageLocation

    storage = build_s3_storage()
    adapters = build_postgres_ledger_adapters(apply_migrations=False, verify=False)
    locations: list[StorageLocation] = []
    with adapters.transactions.operation() as connection:
        cursor = connection.cursor()
        try:
            pattern = f"stage16-{run_id}-%"
            cursor.execute("SELECT document_id FROM documents WHERE title LIKE %s", (pattern,))
            document_ids = [int(row[0]) for row in cursor.fetchall()]
            version_ids: list[int] = []
            file_ids: list[int] = []
            if document_ids:
                cursor.execute(
                    "SELECT document_version_id,file_object_id FROM document_versions "
                    "WHERE document_id=ANY(%s)",
                    (document_ids,),
                )
                versions = tuple(cursor.fetchall())
                version_ids = [int(row[0]) for row in versions]
                file_ids = [int(row[1]) for row in versions]
                cursor.execute(
                    "SELECT storage_provider,storage_namespace,storage_key,file_size "
                    "FROM file_objects WHERE file_object_id=ANY(%s)",
                    (file_ids,),
                )
                locations = [
                    StorageLocation(str(row[0]).lower(), str(row[1]), str(row[2]), int(row[3]))
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    "SELECT event_id::text FROM sync_events "
                    "WHERE payload->>'documentId'=ANY(%s)",
                    ([str(value) for value in document_ids],),
                )
                event_ids = [str(row[0]) for row in cursor.fetchall()]
                if event_ids:
                    cursor.execute(
                        "DELETE FROM sync_delivery_attempts WHERE event_id=ANY(%s::uuid[])",
                        (event_ids,),
                    )
                    cursor.execute("DELETE FROM sync_events WHERE event_id=ANY(%s::uuid[])", (event_ids,))
                if version_ids:
                    for table in ("indexing_events", "indexing_attempts"):
                        cursor.execute(
                            f"DELETE FROM {table} WHERE job_id IN "
                            "(SELECT job_id FROM indexing_jobs WHERE document_version_id=ANY(%s))",
                            (version_ids,),
                        )
                    cursor.execute("DELETE FROM document_vectors WHERE document_version_id=ANY(%s)", (version_ids,))
                    cursor.execute("DELETE FROM document_chunks WHERE document_version_id=ANY(%s)", (version_ids,))
                    cursor.execute("DELETE FROM indexing_jobs WHERE document_version_id=ANY(%s)", (version_ids,))
                cursor.execute("DELETE FROM document_permission_cache WHERE document_id=ANY(%s)", (document_ids,))
                cursor.execute("DELETE FROM collection_documents WHERE document_id=ANY(%s)", (document_ids,))
                cursor.execute("DELETE FROM direct_permissions WHERE document_id=ANY(%s)", (document_ids,))
                if version_ids:
                    cursor.execute("DELETE FROM document_versions WHERE document_version_id=ANY(%s)", (version_ids,))
                cursor.execute("DELETE FROM documents WHERE document_id=ANY(%s)", (document_ids,))
                if file_ids:
                    cursor.execute("DELETE FROM file_objects WHERE file_object_id=ANY(%s)", (file_ids,))
            cursor.execute("DELETE FROM search_requests WHERE query_text LIKE %s", (f"%{run_id}%",))
            cursor.execute("SELECT worker_id FROM indexing_workers WHERE instance_id LIKE %s", (f"{run_id}-%",))
            worker_ids = [int(row[0]) for row in cursor.fetchall()]
            if worker_ids:
                cursor.execute("DELETE FROM indexing_workers WHERE worker_id=ANY(%s)", (worker_ids,))
            email = f"stage16-{run_id}@example.invalid"
            cursor.execute("SELECT user_id FROM users WHERE email=%s", (email,))
            user_ids = [int(row[0]) for row in cursor.fetchall()]
            if user_ids:
                cursor.execute("DELETE FROM mcp_tokens WHERE owner_user_id=ANY(%s)", (user_ids,))
                cursor.execute("DELETE FROM user_roles WHERE user_id=ANY(%s) OR granted_by_user_id=ANY(%s)", (user_ids, user_ids))
                cursor.execute("DELETE FROM collections WHERE owner_user_id=ANY(%s)", (user_ids,))
                cursor.execute("DELETE FROM users WHERE user_id=ANY(%s)", (user_ids,))
            cursor.execute(
                "DELETE FROM departments WHERE department_id=%s AND name=%s",
                (int(run_id[:10], 16), f"stage16-{run_id}"),
            )
        finally:
            cursor.close()
    for location in locations:
        storage.delete(location)


def run_scenario(item: str, run_id: str) -> dict[str, object]:
    try:
        if item == "16-1":
            result = scenario_16_1(run_id)
        elif item == "16-2":
            result = scenario_16_2(run_id)
        elif item == "16-3":
            result = scenario_16_3(run_id)
        elif item == "16-4a":
            result = scenario_16_4(run_id, "before")
        elif item == "16-4b":
            result = scenario_16_4(run_id, "after")
        elif item == "16-5":
            result = scenario_outage(run_id, multi_az=False)
        else:
            result = scenario_outage(run_id, multi_az=True)
    except Blocked as error:
        result = {"status": "BLOCKED", "reason": str(error)}
    except AssertionError as error:
        result = {
            "status": "FAIL",
            "errorClass": "AssertionError",
            "reason": str(error),
        }
    except Exception as error:
        result = {"status": "FAIL", "errorClass": type(error).__name__}
    try:
        cleanup_run(run_id)
        result["cleanup"] = "PASS"
    except Exception as error:
        result["cleanup"] = "FAIL"
        result["cleanupErrorClass"] = type(error).__name__
        if result["status"] == "PASS":
            result["status"] = "FAIL"
    return result


def run_parent(scenario: str) -> int:
    missing = tuple(name for name in DB_ENV if not os.environ.get(name))
    safe_environment = os.environ.get("STAGE16_TEST_ENVIRONMENT", "").casefold() in {
        "test",
        "staging",
    }
    selected = SCENARIOS if scenario == "all" else (scenario,)
    root_run_id = uuid4().hex
    emit("STAGE16_START", runId=root_run_id, scenarios=selected)
    results: dict[str, dict[str, object]] = {}
    for item in selected:
        run_id = uuid5(NAMESPACE_URL, f"stage16:{root_run_id}:{item}").hex
        result = (
            {"status": "BLOCKED", "missing": missing, "cleanup": "NOT_NEEDED"}
            if missing
            else {
                "status": "BLOCKED",
                "reason": "test or staging environment is not explicitly selected",
                "cleanup": "NOT_NEEDED",
            }
            if not safe_environment
            else run_scenario(item, run_id)
        )
        results[item] = result
        emit("STAGE16_RESULT", scenario=item, runId=run_id, **result)
    statuses = {result["status"] for result in results.values()}
    if statuses == {"PASS"}:
        return 0
    return 1 if "FAIL" in statuses else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    value.add_argument("--child", choices=("claim", "claim-wait", "commit", "dispatch-wait"))
    value.add_argument("--run-id")
    value.add_argument("--instance-id")
    value.add_argument("--ready")
    value.add_argument("--result")
    value.add_argument("--barrier")
    value.add_argument("--phase", choices=("before", "after"))
    value.add_argument("--title")
    value.add_argument("--user-id")
    value.add_argument("--user-subject")
    value.add_argument("--event-id")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.child == "claim":
        return child_claim(args, wait=False)
    if args.child == "claim-wait":
        return child_claim(args, wait=True)
    if args.child == "commit":
        return child_commit(args)
    if args.child == "dispatch-wait":
        return child_dispatch(args)
    return run_parent(args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
