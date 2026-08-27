"""PostgreSQL persistence for the sync ledger; domain rules stay in SyncService."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from src.infra.postgres.transaction import PostgresTransactionManager
from src.shared.sync import (
    ConsistencyIssueRow,
    ReconciliationRunRow,
    SyncDeliveryAttemptRow,
    SyncEventRow,
    SyncOperatorActionRow,
)


_EVENT_COLUMNS = """
    event_id, idempotency_key, aggregate_type, aggregate_id,
    aggregate_version, event_type, payload, status, occurred_at,
    available_at, max_retries, failure_count, owner_name, claim_token,
    locked_at, lease_expires_at, processed_at, failed_at,
    error_type, error_message
"""


def _value(row: object, index: int, name: str) -> object:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]  # type: ignore[index]


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _payload(value: object) -> object:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value) if isinstance(value, str) else value


def _identifier(value: object) -> int | str:
    text = str(value)
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if isinstance(decoded, bool) or not isinstance(decoded, (int, str)):
        return text
    return decoded


def _identifier_text(value: int | str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("sync aggregate ID must be an integer or string")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _event(row: object | None) -> SyncEventRow | None:
    if row is None:
        return None
    payload = _payload(_value(row, 6, "payload"))
    return SyncEventRow(
        str(_value(row, 0, "event_id")),
        str(_value(row, 1, "idempotency_key")),
        str(_value(row, 2, "aggregate_type")),
        _identifier(_value(row, 3, "aggregate_id")),
        (
            int(_value(row, 4, "aggregate_version"))
            if _value(row, 4, "aggregate_version") is not None
            else None
        ),
        str(_value(row, 5, "event_type")),
        payload,
        _canonical(payload),
        str(_value(row, 7, "status")),
        _value(row, 8, "occurred_at"),  # type: ignore[arg-type]
        _value(row, 9, "available_at"),  # type: ignore[arg-type]
        int(_value(row, 10, "max_retries")),
        int(_value(row, 11, "failure_count")),
        (
            str(_value(row, 12, "owner_name"))
            if _value(row, 12, "owner_name") is not None
            else None
        ),
        (
            str(_value(row, 13, "claim_token"))
            if _value(row, 13, "claim_token") is not None
            else None
        ),
        _value(row, 14, "locked_at"),  # type: ignore[arg-type]
        _value(row, 15, "lease_expires_at"),  # type: ignore[arg-type]
        _value(row, 16, "processed_at"),  # type: ignore[arg-type]
        _value(row, 17, "failed_at"),  # type: ignore[arg-type]
        (
            str(_value(row, 18, "error_type"))
            if _value(row, 18, "error_type") is not None
            else None
        ),
        (
            str(_value(row, 19, "error_message"))
            if _value(row, 19, "error_message") is not None
            else None
        ),
    )


def _attempt(row: object) -> SyncDeliveryAttemptRow:
    return SyncDeliveryAttemptRow(
        str(_value(row, 0, "delivery_attempt_id")),
        str(_value(row, 1, "event_id")),
        int(_value(row, 2, "attempt_no")),
        str(_value(row, 3, "status")),
        _value(row, 4, "started_at"),  # type: ignore[arg-type]
        _value(row, 5, "ended_at"),  # type: ignore[arg-type]
        (
            str(_value(row, 6, "error_type"))
            if _value(row, 6, "error_type") is not None
            else None
        ),
        (
            str(_value(row, 7, "error_message"))
            if _value(row, 7, "error_message") is not None
            else None
        ),
    )


def _issue(row: object | None) -> ConsistencyIssueRow | None:
    if row is None:
        return None
    return ConsistencyIssueRow(
        str(_value(row, 0, "issue_id")),
        str(_value(row, 1, "issue_type")),
        str(_value(row, 2, "severity")),
        str(_value(row, 3, "status")),
        bool(_value(row, 4, "safe_to_repair")),
        _value(row, 5, "created_at"),  # type: ignore[arg-type]
        _value(row, 6, "updated_at"),  # type: ignore[arg-type]
        (
            str(_value(row, 7, "ignored_reason"))
            if _value(row, 7, "ignored_reason") is not None
            else None
        ),
    )


class PostgresSyncStore:
    def __init__(self, transactions: PostgresTransactionManager) -> None:
        self._transactions = transactions

    def transaction(self) -> AbstractContextManager[None]:
        return self._transactions.transaction()

    def insert_event(self, event: SyncEventRow) -> bool:
        sql = """
            INSERT INTO sync_events (
                event_id, idempotency_key, aggregate_type, aggregate_id,
                aggregate_version, event_type, payload, status, occurred_at,
                available_at, max_retries, failure_count, owner_name,
                claim_token, locked_at, lease_expires_at, processed_at,
                failed_at, error_type, error_message
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING event_id
        """
        values = (
            event.id,
            event.idempotency_key,
            event.aggregate_type,
            _identifier_text(event.aggregate_id),
            event.aggregate_version,
            event.event_type,
            event.canonical_payload,
            event.status,
            event.occurred_at,
            event.available_at,
            event.max_retries,
            event.failure_count,
            event.owner_name,
            event.claim_token,
            event.locked_at,
            event.lease_expires_at,
            event.processed_at,
            event.failed_at,
            event.error_type,
            event.error_message,
        )
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, values)
                return cursor.fetchone() is not None
            finally:
                cursor.close()

    def get_event(
        self,
        event_id: str,
        *,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> SyncEventRow | None:
        if skip_locked and not for_update:
            raise ValueError("skip_locked requires for_update")
        suffix = " FOR UPDATE" if for_update else ""
        if skip_locked:
            suffix += " SKIP LOCKED"
        return self._one_event(
            f"SELECT {_EVENT_COLUMNS} FROM sync_events WHERE event_id = %s{suffix}",
            (event_id,),
        )

    def get_event_by_key(
        self, idempotency_key: str, *, for_update: bool = False
    ) -> SyncEventRow | None:
        suffix = " FOR UPDATE" if for_update else ""
        return self._one_event(
            f"SELECT {_EVENT_COLUMNS} FROM sync_events WHERE idempotency_key = %s{suffix}",
            (idempotency_key,),
        )

    def _one_event(self, sql: str, parameters: tuple[object, ...]) -> SyncEventRow | None:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, parameters)
                return _event(cursor.fetchone())
            finally:
                cursor.close()

    def list_events(self) -> tuple[SyncEventRow, ...]:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM sync_events "
                    "ORDER BY occurred_at, event_id"
                )
                return tuple(_event(row) for row in cursor.fetchall())  # type: ignore[misc]
            finally:
                cursor.close()

    def save_event(self, event: SyncEventRow) -> None:
        sql = """
            UPDATE sync_events SET
                status = %s, available_at = %s, max_retries = %s,
                failure_count = %s, owner_name = %s, claim_token = %s,
                locked_at = %s, lease_expires_at = %s, processed_at = %s,
                failed_at = %s, error_type = %s, error_message = %s
            WHERE event_id = %s
        """
        self._update_one(sql, (
            event.status, event.available_at, event.max_retries,
            event.failure_count, event.owner_name, event.claim_token,
            event.locked_at, event.lease_expires_at, event.processed_at,
            event.failed_at, event.error_type, event.error_message, event.id,
        ), event.id)

    def insert_attempt(self, attempt: SyncDeliveryAttemptRow) -> None:
        self._execute(
            """
                INSERT INTO sync_delivery_attempts (
                    delivery_attempt_id, event_id, attempt_no, status,
                    started_at, ended_at, error_type, error_message
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                attempt.id, attempt.event_id, attempt.attempt_no, attempt.status,
                attempt.started_at, attempt.ended_at, attempt.error_type,
                attempt.error_message,
            ),
        )

    def list_attempts(self, event_id: str) -> tuple[SyncDeliveryAttemptRow, ...]:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                        SELECT delivery_attempt_id, event_id, attempt_no, status,
                               started_at, ended_at, error_type, error_message
                        FROM sync_delivery_attempts
                        WHERE event_id = %s
                        ORDER BY attempt_no
                    """,
                    (event_id,),
                )
                return tuple(_attempt(row) for row in cursor.fetchall())
            finally:
                cursor.close()

    def save_attempt(self, attempt: SyncDeliveryAttemptRow) -> None:
        self._update_one(
            """
                UPDATE sync_delivery_attempts
                SET status = %s, ended_at = %s, error_type = %s, error_message = %s
                WHERE delivery_attempt_id = %s
            """,
            (
                attempt.status, attempt.ended_at, attempt.error_type,
                attempt.error_message, attempt.id,
            ),
            attempt.id,
        )

    def insert_issue(self, issue: ConsistencyIssueRow) -> None:
        self._execute(
            """
                INSERT INTO consistency_issues (
                    issue_id, issue_type, severity, status, safe_to_repair,
                    created_at, updated_at, ignored_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                issue.id, issue.issue_type, issue.severity, issue.status,
                issue.safe_to_repair, issue.created_at, issue.updated_at,
                issue.ignored_reason,
            ),
        )

    def get_issue(
        self, issue_id: str, *, for_update: bool = False
    ) -> ConsistencyIssueRow | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                        SELECT issue_id, issue_type, severity, status,
                               safe_to_repair, created_at, updated_at, ignored_reason
                        FROM consistency_issues WHERE issue_id = %s
                    """ + suffix,
                    (issue_id,),
                )
                return _issue(cursor.fetchone())
            finally:
                cursor.close()

    def list_issues(self) -> tuple[ConsistencyIssueRow, ...]:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                        SELECT issue_id, issue_type, severity, status,
                               safe_to_repair, created_at, updated_at, ignored_reason
                        FROM consistency_issues ORDER BY created_at, issue_id
                    """
                )
                return tuple(_issue(row) for row in cursor.fetchall())  # type: ignore[misc]
            finally:
                cursor.close()

    def save_issue(self, issue: ConsistencyIssueRow) -> None:
        self._update_one(
            """
                UPDATE consistency_issues
                SET status = %s, safe_to_repair = %s, updated_at = %s,
                    ignored_reason = %s
                WHERE issue_id = %s
            """,
            (
                issue.status, issue.safe_to_repair, issue.updated_at,
                issue.ignored_reason, issue.id,
            ),
            issue.id,
        )

    def insert_action(self, action: SyncOperatorActionRow) -> None:
        self._execute(
            """
                INSERT INTO operator_actions (
                    action_id, action_type, target_type, target_id,
                    actor_id, occurred_at, reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                action.id, action.action_type, action.target_type,
                action.target_id, str(action.actor_id), action.occurred_at,
                action.reason,
            ),
        )

    def insert_reconciliation(self, run: ReconciliationRunRow) -> None:
        self._execute(
            """
                INSERT INTO reconciliation_runs (
                    reconciliation_id, mode, cursor, status,
                    started_at, completed_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run.id, run.mode, run.cursor, run.status,
                run.started_at, run.completed_at,
            ),
        )

    def save_reconciliation(self, run: ReconciliationRunRow) -> None:
        self._update_one(
            """
                UPDATE reconciliation_runs
                SET status = %s, completed_at = %s
                WHERE reconciliation_id = %s
            """,
            (run.status, run.completed_at, run.id),
            run.id,
        )

    def _execute(self, sql: str, parameters: Sequence[object]) -> None:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, tuple(parameters))
            finally:
                cursor.close()

    def _update_one(
        self, sql: str, parameters: Sequence[object], identifier: str
    ) -> None:
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(sql, tuple(parameters))
                if cursor.rowcount != 1:
                    raise KeyError(identifier)
            finally:
                cursor.close()
