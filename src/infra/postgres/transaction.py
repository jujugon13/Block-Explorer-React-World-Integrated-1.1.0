"""Ambient PostgreSQL transaction boundary shared by all ledger stores."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from src.shared.database import CommitOutcomeUnknown


ConnectionFactory = Callable[[], Any]
ConnectionErrorPredicate = Callable[[BaseException], bool]


class TransactionRollbackOnlyError(RuntimeError):
    """A nested failure was caught, but the enclosing transaction must abort."""


class PostgresConnectionError(ConnectionError):
    """A connection could not be opened; driver details stay outside logs."""


@dataclass(slots=True)
class _AmbientTransaction:
    connection: Any
    depth: int = 1
    rollback_only: bool = False


def _default_connection_error(error: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))


def _execute(connection: Any, sql: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def _safe_rollback(connection: Any) -> None:
    try:
        connection.rollback()
    except BaseException:
        pass


def _safe_close(connection: Any) -> None:
    try:
        connection.close()
    except BaseException:
        pass


class PostgresTransactionManager:
    """Use one connection for nested stores and commit only at the outer edge."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        is_connection_error: ConnectionErrorPredicate | None = None,
    ) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory
        self._is_connection_error = is_connection_error or _default_connection_error
        self._ambient: ContextVar[_AmbientTransaction | None] = ContextVar(
            "vectorshelf_postgres_transaction",
            default=None,
        )

    def current_connection(self) -> Any:
        state = self._ambient.get()
        if state is None:
            raise RuntimeError("no ambient PostgreSQL transaction")
        return state.connection

    @contextmanager
    def transaction(self):
        state = self._ambient.get()
        if state is not None:
            state.depth += 1
            try:
                yield
            except BaseException:
                state.rollback_only = True
                raise
            finally:
                state.depth -= 1
            return

        try:
            connection = self._connection_factory()
        except Exception:
            raise PostgresConnectionError("PostgreSQL connection failed") from None
        state = _AmbientTransaction(connection)
        token = self._ambient.set(state)
        try:
            try:
                _execute(
                    connection,
                    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
                )
                yield
            except BaseException:
                state.rollback_only = True
                _safe_rollback(connection)
                raise

            if state.rollback_only:
                _safe_rollback(connection)
                raise TransactionRollbackOnlyError(
                    "nested transaction failure marked the transaction rollback-only"
                )

            try:
                connection.commit()
            except BaseException as error:
                if self._is_connection_error(error):
                    raise CommitOutcomeUnknown(
                        "PostgreSQL commit outcome is unknown"
                    ) from None
                _safe_rollback(connection)
                raise
        finally:
            self._ambient.reset(token)
            _safe_close(connection)

    @contextmanager
    def operation(self):
        """Open a short transaction or reuse the caller's ambient connection."""

        with self.transaction():
            yield self.current_connection()
