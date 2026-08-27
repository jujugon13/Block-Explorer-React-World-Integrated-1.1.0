"""Authorized RDS interruption/failover and live uvicorn probe helpers."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class Blocked(RuntimeError):
    """A destructive check lacks an explicit safe precondition."""


@dataclass(frozen=True, slots=True)
class RdsTarget:
    resource_type: str
    identifier: str
    arn: str
    endpoint: str
    writer: str
    primary_az: str
    status: str


def _required(*names: str) -> dict[str, str]:
    missing = tuple(name for name in names if not os.environ.get(name))
    if missing:
        raise Blocked("missing=" + ",".join(missing))
    return {name: os.environ[name] for name in names}


def _environment() -> str:
    value = os.environ.get("STAGE16_TEST_ENVIRONMENT", "").casefold()
    if value not in {"test", "staging"}:
        raise Blocked("test environment is not explicitly selected")
    return value


def _client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise Blocked("boto3 is unavailable") from None
    return boto3.client(
        "rds",
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _aws(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as error:
        code = ""
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
        if type(error).__name__ in {
            "NoCredentialsError",
            "PartialCredentialsError",
            "CredentialRetrievalError",
        } or code in {
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedOperation",
            "UnrecognizedClientException",
        }:
            raise Blocked("AWS credentials or permission are unavailable") from None
        raise


def _endpoint_matches(endpoint: str) -> bool:
    configured = os.environ.get("DB_HOST", "")
    return bool(configured) and endpoint.rstrip(".").casefold() == configured.rstrip(".").casefold()


def _nonproduction(client, arn: str, expected: str) -> None:
    key = os.environ.get("STAGE16_ENVIRONMENT_TAG_KEY", "Environment")
    result = _aws(client.list_tags_for_resource, ResourceName=arn)
    tags = {
        str(item.get("Key", "")): str(item.get("Value", "")).casefold()
        for item in result.get("TagList", ())
    }
    if tags.get(key) != expected:
        raise Blocked("RDS nonproduction tag was not verified")


def _instance(client, identifier: str) -> dict[str, object]:
    result = _aws(client.describe_db_instances, DBInstanceIdentifier=identifier)
    rows = result.get("DBInstances", ())
    if len(rows) != 1:
        raise Blocked("RDS instance was not found uniquely")
    return rows[0]


def _cluster(client, identifier: str) -> dict[str, object]:
    result = _aws(client.describe_db_clusters, DBClusterIdentifier=identifier)
    rows = result.get("DBClusters", ())
    if len(rows) != 1:
        raise Blocked("RDS cluster was not found uniquely")
    return rows[0]


def _postgres(engine: object) -> bool:
    return str(engine).casefold().startswith("postgres")


def single_az_target():
    values = _required(
        "STAGE16_SINGLE_AZ_RDS_RESOURCE_ID",
        "STAGE16_SINGLE_AZ_REBOOT_AUTHORIZED",
        "STAGE16_TEST_ENVIRONMENT",
    )
    if values["STAGE16_SINGLE_AZ_REBOOT_AUTHORIZED"] != "YES":
        raise Blocked("Single-AZ reboot is not explicitly authorized")
    expected = _environment()
    client = _client()
    row = _instance(client, values["STAGE16_SINGLE_AZ_RDS_RESOURCE_ID"])
    endpoint = str(row.get("Endpoint", {}).get("Address", ""))
    if (
        not _postgres(row.get("Engine"))
        or bool(row.get("MultiAZ"))
        or not _endpoint_matches(endpoint)
    ):
        raise Blocked("Single-AZ PostgreSQL target did not match DB_HOST")
    arn = str(row.get("DBInstanceArn", ""))
    _nonproduction(client, arn, expected)
    target = RdsTarget(
        "db-instance",
        values["STAGE16_SINGLE_AZ_RDS_RESOURCE_ID"],
        arn,
        endpoint,
        values["STAGE16_SINGLE_AZ_RDS_RESOURCE_ID"],
        str(row.get("AvailabilityZone", "")),
        str(row.get("DBInstanceStatus", "")),
    )

    def trigger() -> None:
        _aws(client.reboot_db_instance, DBInstanceIdentifier=target.identifier)

    return client, target, trigger


def multi_az_target():
    values = _required(
        "STAGE16_RDS_RESOURCE_TYPE",
        "STAGE16_RDS_RESOURCE_ID",
        "STAGE16_FAILOVER_AUTHORIZED",
        "STAGE16_TEST_ENVIRONMENT",
    )
    if values["STAGE16_FAILOVER_AUTHORIZED"] != "YES":
        raise Blocked("failover is not explicitly authorized")
    expected = _environment()
    client = _client()
    kind = values["STAGE16_RDS_RESOURCE_TYPE"]
    identifier = values["STAGE16_RDS_RESOURCE_ID"]
    if kind == "db-instance":
        row = _instance(client, identifier)
        endpoint = str(row.get("Endpoint", {}).get("Address", ""))
        if (
            not _postgres(row.get("Engine"))
            or not bool(row.get("MultiAZ"))
            or not _endpoint_matches(endpoint)
        ):
            raise Blocked("Multi-AZ PostgreSQL instance did not match DB_HOST")
        arn = str(row.get("DBInstanceArn", ""))
        _nonproduction(client, arn, expected)
        target = RdsTarget(
            kind,
            identifier,
            arn,
            endpoint,
            identifier,
            str(row.get("AvailabilityZone", "")),
            str(row.get("DBInstanceStatus", "")),
        )

        def trigger() -> None:
            _aws(
                client.reboot_db_instance,
                DBInstanceIdentifier=identifier,
                ForceFailover=True,
            )

        return client, target, trigger
    if kind != "multi-az-db-cluster":
        raise Blocked("unsupported RDS resource type")
    row = _cluster(client, identifier)
    endpoint = str(row.get("Endpoint", ""))
    members = tuple(row.get("DBClusterMembers", ()))
    writer = next((item for item in members if item.get("IsClusterWriter")), None)
    zones = tuple(row.get("AvailabilityZones", ()))
    if (
        not _postgres(row.get("Engine"))
        or writer is None
        or len(zones) < 2
        or not _endpoint_matches(endpoint)
    ):
        raise Blocked("Multi-AZ PostgreSQL cluster did not match DB_HOST")
    writer_id = str(writer.get("DBInstanceIdentifier", ""))
    writer_row = _instance(client, writer_id)
    arn = str(row.get("DBClusterArn", ""))
    _nonproduction(client, arn, expected)
    target = RdsTarget(
        kind,
        identifier,
        arn,
        endpoint,
        writer_id,
        str(writer_row.get("AvailabilityZone", "")),
        str(row.get("Status", "")),
    )

    def trigger() -> None:
        _aws(client.failover_db_cluster, DBClusterIdentifier=identifier)

    return client, target, trigger


def refresh(client, target: RdsTarget) -> RdsTarget:
    if target.resource_type == "db-instance":
        row = _instance(client, target.identifier)
        return RdsTarget(
            target.resource_type,
            target.identifier,
            target.arn,
            str(row.get("Endpoint", {}).get("Address", "")),
            target.identifier,
            str(row.get("AvailabilityZone", "")),
            str(row.get("DBInstanceStatus", "")),
        )
    row = _cluster(client, target.identifier)
    writer = next(
        (item for item in row.get("DBClusterMembers", ()) if item.get("IsClusterWriter")),
        None,
    )
    if writer is None:
        raise RuntimeError("RDS writer is unavailable")
    writer_id = str(writer.get("DBInstanceIdentifier", ""))
    writer_row = _instance(client, writer_id)
    return RdsTarget(
        target.resource_type,
        target.identifier,
        target.arn,
        str(row.get("Endpoint", "")),
        writer_id,
        str(writer_row.get("AvailabilityZone", "")),
        str(row.get("Status", "")),
    )


def _http_probe(prefix: str, run_id: str) -> bool:
    url = os.environ.get(f"STAGE16_{prefix}_URL")
    if not url:
        raise Blocked(f"missing=STAGE16_{prefix}_URL")
    method = os.environ.get(f"STAGE16_{prefix}_METHOD", "GET").upper()
    body = os.environ.get(f"STAGE16_{prefix}_BODY")
    if prefix == "SEARCH" and (body is None or "{run_id}" not in body):
        raise Blocked("search probe body must contain the run_id placeholder")
    data = body.replace("{run_id}", run_id).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if authorization := os.environ.get("STAGE16_API_AUTHORIZATION"):
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
            server = response.headers.get("Server", "").casefold()
            return 200 <= response.status < 300 and "uvicorn" in server
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
        return False


def probe(run_id: str) -> tuple[bool, bool, bool]:
    from src.infra.postgres.config import PostgresConfig, connect

    database = False
    try:
        connection = connect(PostgresConfig.from_env())
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1")
                database = cursor.fetchone() == (1,)
            finally:
                cursor.close()
        finally:
            connection.close()
    except Exception:
        database = False
    return database, _http_probe("API", run_id), _http_probe("SEARCH", run_id)


def interrupt_and_observe(
    client, target: RdsTarget, trigger, run_id: str, *, require_topology: bool = False
) -> dict[str, object]:
    if probe(run_id) != (True, True, True):
        raise AssertionError("pre-outage DB/API/search probe failed")
    timeout = float(os.environ.get("STAGE16_OUTAGE_TIMEOUT_SECONDS", "900"))
    if not 30 <= timeout <= 3600:
        raise Blocked("outage timeout is outside the safe range")
    started = time.monotonic()
    trigger()
    failures = [False, False, False]
    observed = (False, False, False)
    current = target
    recovered: dict[str, object] | None = None
    while time.monotonic() - started < timeout:
        if recovered is None:
            observed = probe(run_id)
            failures = [old or not value for old, value in zip(failures, observed)]
        try:
            current = refresh(client, target)
        except Exception:
            pass
        topology = bool(
            current.writer != target.writer
            or current.primary_az != target.primary_az
        )
        if (
            recovered is None
            and all(failures)
            and all(observed)
            and current.status.casefold() == "available"
        ):
            if not _endpoint_matches(current.endpoint):
                raise AssertionError("RDS endpoint changed")
            recovered = {
                "outageObserved": True,
                "apiFailureObserved": failures[1],
                "searchFailureObserved": failures[2],
                "recoverySeconds": round(time.monotonic() - started, 3),
            }
        if recovered is not None and (topology or not require_topology):
            return {
                **recovered,
                "writerChanged": current.writer != target.writer,
                "primaryAzChanged": current.primary_az != target.primary_az,
            }
        time.sleep(0.5)
    names = ("DB", "API", "search")
    if not all(failures):
        missing = ",".join(name for name, failed in zip(names, failures) if not failed)
        raise AssertionError(f"outage was not observed by {missing}")
    if recovered is not None:
        raise AssertionError("RDS topology change was not observed after recovery")
    missing = ",".join(name for name, recovered in zip(names, observed) if not recovered)
    raise AssertionError(
        f"recovery was not observed by {missing or 'RDS status'}"
    )
