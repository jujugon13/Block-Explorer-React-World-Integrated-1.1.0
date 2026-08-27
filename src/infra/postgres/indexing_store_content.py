"""Attempt, event, chunk, model, and vector persistence methods."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from src.shared.indexing import ChunkRecord

from .indexing_rows import (
    ATTEMPT_COLUMNS,
    EVENT_COLUMNS,
    MODEL_COLUMNS,
    VECTOR_COLUMNS,
    _value,
    attempt_row,
    event_row,
    model_row,
    vector_row,
    vector_text,
)


_ATTEMPT_SELECT = ", ".join(ATTEMPT_COLUMNS)
_EVENT_SELECT = ", ".join(EVENT_COLUMNS)
_MODEL_SELECT = (
    "embedding_model_id, model_name AS name, dimension, active, searchable, "
    "provider, model_version"
)
_VECTOR_SELECT = (
    "vector_id, document_version_id, chunk_index, embedding_model_id, "
    "embedding::text AS embedding_text, status"
)
_CHUNK_COLUMNS = (
    "document_version_id", "chunk_index", "start_offset", "end_offset",
    "content", "content_sha256", "token_estimate", "page_number", "section_title",
)


def _chunk_row(row: Any) -> ChunkRecord:
    values = [_value(row, _CHUNK_COLUMNS, name) for name in _CHUNK_COLUMNS]
    return ChunkRecord(
        int(values[0]), int(values[1]), int(values[2]), int(values[3]),
        str(values[4]), str(values[5]), int(values[6]), values[7], values[8],
    )


class PostgresIndexingContentMixin:
    def get_attempt(self, attempt_id: int):
        row = self._fetchone(
            f"SELECT {_ATTEMPT_SELECT} FROM indexing_attempts WHERE attempt_id = %s",
            (attempt_id,),
        )
        return attempt_row(row) if row is not None else None

    def lock_attempt(self, attempt_id: int):
        row = self._fetchone(
            f"SELECT {_ATTEMPT_SELECT} FROM indexing_attempts "
            "WHERE attempt_id = %s FOR UPDATE",
            (attempt_id,),
        )
        return attempt_row(row) if row is not None else None

    def list_attempts(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_ATTEMPT_SELECT} FROM indexing_attempts ORDER BY attempt_id"
        )
        return tuple(attempt_row(row) for row in rows)

    @staticmethod
    def _attempt_result(attempt: Any) -> tuple[object, object, object]:
        result = attempt.failure_result
        return result if result is not None else (None, None, None)

    def insert_attempt(self, attempt: Any) -> None:
        result_status, result_count, result_next = self._attempt_result(attempt)
        self._execute(
            """
            INSERT INTO indexing_attempts (
                attempt_id, job_id, attempt_no, worker_id, claim_token, status,
                started_at, ended_at, duration_ms, failure_type, error_message,
                result_job_status, result_retry_count, result_next_run_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                attempt.id, attempt.job_id, attempt.attempt_no, attempt.worker_id,
                attempt.claim_token, attempt.status, attempt.started_at, attempt.ended_at,
                attempt.duration_ms, attempt.failure_type, attempt.error_message,
                result_status, result_count, result_next,
            ),
        )

    def save_attempt(self, attempt: Any) -> None:
        result_status, result_count, result_next = self._attempt_result(attempt)
        self._execute(
            """
            UPDATE indexing_attempts
               SET status = %s,
                   ended_at = %s,
                   duration_ms = %s,
                   failure_type = %s,
                   error_message = %s,
                   result_job_status = %s,
                   result_retry_count = %s,
                   result_next_run_at = %s
             WHERE attempt_id = %s
            """,
            (
                attempt.status, attempt.ended_at, attempt.duration_ms,
                attempt.failure_type, attempt.error_message, result_status,
                result_count, result_next, attempt.id,
            ),
        )

    def list_events(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_EVENT_SELECT} FROM indexing_events ORDER BY indexing_event_id"
        )
        return tuple(event_row(row) for row in rows)

    def insert_event(self, event: Any) -> None:
        metadata = json.dumps(event.metadata, sort_keys=True, separators=(",", ":"))
        self._execute(
            """
            INSERT INTO indexing_events (
                indexing_event_id, job_id, event_type, occurred_at, metadata
            ) VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (event.id, event.job_id, event.event_type, event.occurred_at, metadata),
        )

    def get_model(self, model_id: int):
        row = self._fetchone(
            f"SELECT {_MODEL_SELECT} FROM embedding_models WHERE embedding_model_id = %s",
            (model_id,),
        )
        return model_row(row) if row is not None else None

    def list_models(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_MODEL_SELECT} FROM embedding_models ORDER BY embedding_model_id"
        )
        return tuple(model_row(row) for row in rows)

    def insert_model(self, model: Any) -> None:
        self._execute(
            """
            INSERT INTO embedding_models (
                embedding_model_id, model_name, dimension, active, searchable,
                provider, model_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                model.id, model.name, model.dimension, model.active,
                model.searchable, model.provider, model.model_version,
            ),
        )

    def save_model(self, model: Any) -> None:
        self._execute(
            """
            UPDATE embedding_models
               SET model_name = %s, dimension = %s, active = %s, searchable = %s,
                   provider = %s, model_version = %s
             WHERE embedding_model_id = %s
            """,
            (
                model.name, model.dimension, model.active, model.searchable,
                model.provider, model.model_version, model.id,
            ),
        )

    def get_chunks(self, version_id: int) -> tuple[ChunkRecord, ...]:
        rows = self._fetchall(
            """
            SELECT document_version_id, chunk_index, start_offset, end_offset,
                   content, content_sha256, token_estimate, page_number, section_title
              FROM document_chunks
             WHERE document_version_id = %s
             ORDER BY chunk_index
            """,
            (version_id,),
        )
        return tuple(_chunk_row(row) for row in rows)

    def save_chunks(self, version_id: int, chunks: tuple[ChunkRecord, ...]) -> None:
        self._execute(
            "DELETE FROM document_chunks WHERE document_version_id = %s",
            (version_id,),
        )
        self._executemany(
            """
            INSERT INTO document_chunks (
                document_version_id, chunk_index, start_offset, end_offset,
                content, content_sha256, token_estimate, page_number, section_title
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            tuple(
                (
                    version_id, item.index, item.start, item.end, item.text,
                    item.text_sha256, item.token_estimate, item.page_number,
                    item.section_title,
                )
                for item in chunks
            ),
        )

    def list_vectors(self) -> tuple[Any, ...]:
        rows = self._fetchall(
            f"SELECT {_VECTOR_SELECT} FROM document_vectors ORDER BY vector_id"
        )
        return tuple(vector_row(row) for row in rows)

    def insert_vectors(self, vectors: tuple[Any, ...]) -> None:
        self._executemany(
            """
            INSERT INTO document_vectors (
                vector_id, document_version_id, chunk_index,
                embedding_model_id, embedding, status
            ) VALUES (%s, %s, %s, %s, %s::vector, %s)
            """,
            tuple(
                (
                    item.id, item.version_id, item.chunk_index, item.model_id,
                    vector_text(item.values), item.status,
                )
                for item in vectors
            ),
        )

    def save_vector(self, vector: Any) -> None:
        self._execute(
            """
            UPDATE document_vectors
               SET embedding = %s::vector, status = %s
             WHERE vector_id = %s
            """,
            (vector_text(vector.values), vector.status, vector.id),
        )
