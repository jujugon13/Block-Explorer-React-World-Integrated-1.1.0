"""Shared indexing state-machine rules."""

from src.shared import PublicError


RETRYABLE_FAILURES = frozenset(
    {
        "STORAGE_UNAVAILABLE",
        "EMBEDDING_PROVIDER_UNAVAILABLE",
        "EMBEDDING_PROVIDER_OVERLOADED",
        "EMBEDDING_PROVIDER_TIMEOUT",
        "EMBEDDING_PROVIDER_CIRCUIT_OPEN",
        "WORKER_INTERNAL_ERROR",
    }
)
FAILURE_TYPES = RETRYABLE_FAILURES | {
    "STORAGE_CONFIGURATION_INVALID",
    "STORAGE_OBJECT_MISSING",
    "DOCUMENT_CONTENT_INVALID",
    "EMBEDDING_REQUEST_INVALID",
    "EMBEDDING_RESULT_INVALID",
    "INDEXING_STATE_INCONSISTENT",
}
LOSS_CODES = frozenset(
    {
        "WORKER-001",
        "WORKER-002",
        "EMBEDDING-JOB-001",
        "EMBEDDING-JOB-002",
        "EMBEDDING-JOB-003",
        "EMBEDDING-JOB-004",
        "EMBEDDING-JOB-005",
        "EMBEDDING-JOB-006",
        "EMBEDDING-JOB-007",
    }
)


def fail(code: str) -> None:
    raise PublicError(code)
