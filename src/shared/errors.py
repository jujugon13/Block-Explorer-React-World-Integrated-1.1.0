from collections.abc import Iterable
from typing import NoReturn


class PublicError(Exception):
    """A domain failure identified by the public error ledger."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


class BodyValidationError(Exception):
    """Ordered JSON-body violations; only the first is public."""

    def __init__(self, violations: Iterable[tuple[str, str]]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise ValueError("At least one validation violation is required")
        super().__init__(self.violations[0])


def body_violation(field: str, detail: str) -> NoReturn:
    """Raise one field-specific body constraint failure."""

    raise BodyValidationError(((field, detail),))
