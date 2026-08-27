"""PostgreSQL search-history writer, retention, and operations projection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from src.infra.postgres.transaction import PostgresTransactionManager
from src.shared.ops import OpsSearchSnapshot
from src.shared.search import SearchHistoryBundle


def _value(row: object, index: int, name: str) -> object:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]  # type: ignore[index]


class PostgresSearchHistoryStore:
    def __init__(self, transactions: PostgresTransactionManager) -> None:
        self._transactions = transactions

    def record(self, bundle: SearchHistoryBundle) -> None:
        """Insert one request and its optional answer/citations atomically."""

        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                search = bundle.search
                cursor.execute(
                    """
                        INSERT INTO search_requests (
                            requester_id, query_text, requested_at, duration_ms,
                            results_count, status, settings_hash
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING search_id
                    """,
                    (
                        search.requester_id,
                        search.query,
                        search.requested_at,
                        search.duration_ms,
                        search.results_count,
                        search.status,
                        search.settings_hash,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("search history insert returned no ID")
                search_id = int(_value(row, 0, "search_id"))

                if bundle.answer is not None:
                    cursor.execute(
                        """
                            INSERT INTO search_answers (
                                search_id, answer_text, status
                            ) VALUES (%s, %s, %s)
                        """,
                        (
                            search_id,
                            bundle.answer.answer,
                            bundle.answer.status,
                        ),
                    )

                if bundle.citations:
                    cursor.executemany(
                        """
                            INSERT INTO search_citations (
                                search_id, rank, chunk_id, document_id
                            ) VALUES (%s, %s, %s, %s)
                        """,
                        tuple(
                            (
                                search_id,
                                citation.rank,
                                citation.chunk_id,
                                citation.document_id,
                            )
                            for citation in bundle.citations
                        ),
                    )
            finally:
                cursor.close()

    def purge_before(self, cutoff: datetime) -> None:
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "DELETE FROM search_requests WHERE requested_at < %s",
                    (cutoff,),
                )
            finally:
                cursor.close()

    def ops_search_snapshots(
        self, now: datetime
    ) -> tuple[OpsSearchSnapshot, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("operations instants must be timezone-aware")
        with self._transactions.operation() as connection:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                        SELECT requested_at
                        FROM search_requests
                        ORDER BY requested_at, search_id
                    """
                )
                moments = tuple(
                    _value(row, 0, "requested_at") for row in cursor.fetchall()
                )
            finally:
                cursor.close()
        if any(
            not isinstance(moment, datetime)
            or moment.tzinfo is None
            or moment.utcoffset() is None
            for moment in moments
        ):
            raise ValueError("operations instants must be timezone-aware")
        return tuple(OpsSearchSnapshot(moment) for moment in moments)
