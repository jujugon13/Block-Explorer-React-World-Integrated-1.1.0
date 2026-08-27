"""Ordered, checksummed SQL migrations for the PostgreSQL deployment ledger."""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    PostgresConfig,
    PostgresConfigurationError,
    PostgresDependencyError,
    connect,
)


MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9][a-z0-9_-]*)\.sql$")
MIGRATIONS_DIRECTORY = Path(__file__).with_name("migrations")
HISTORY_TABLE = "vectorshelf_schema_migrations"
ADVISORY_LOCK_KEY = 0x564543544F525348  # ASCII "VECTORSH", signed bigint safe.


class MigrationError(RuntimeError):
    """A migration set is invalid or could not be applied safely."""


class MigrationCommitUnknown(MigrationError):
    """A disconnected commit could not be resolved from migration history."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    filename: str
    sha256: str
    sql: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    discovered: tuple[str, ...]
    applied: tuple[str, ...]
    already_applied: tuple[str, ...]


ConnectionFactory = Callable[[], Any]
ConnectionErrorPredicate = Callable[[BaseException], bool]


def discover_migrations(directory: str | Path = MIGRATIONS_DIRECTORY) -> tuple[Migration, ...]:
    root = Path(directory)
    if not root.is_dir():
        raise MigrationError("migration directory is missing")
    sql_files = sorted(path for path in root.iterdir() if path.suffix == ".sql")
    if not sql_files:
        raise MigrationError("no SQL migrations were found")

    migrations: list[Migration] = []
    for path in sql_files:
        matched = MIGRATION_NAME.fullmatch(path.name)
        if matched is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        raw = path.read_bytes()
        try:
            sql = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise MigrationError(f"migration is not UTF-8: {path.name}") from None
        if not sql.strip():
            raise MigrationError(f"migration is empty: {path.name}")
        migrations.append(
            Migration(
                int(matched.group(1)),
                path.name,
                hashlib.sha256(raw).hexdigest(),
                sql,
            )
        )

    expected = list(range(1, len(migrations) + 1))
    actual = [migration.version for migration in migrations]
    if actual != expected:
        raise MigrationError("migration versions must be continuous from 0001")
    return tuple(migrations)


def _cursor(connection: Any):
    return connection.cursor()


def _close_cursor(cursor: Any) -> None:
    close = getattr(cursor, "close", None)
    if callable(close):
        close()


def _execute(connection: Any, sql: str, parameters: object = None) -> Any:
    cursor = _cursor(connection)
    try:
        if parameters is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, parameters)
        return cursor
    except Exception:
        _close_cursor(cursor)
        raise


def _fetchone(connection: Any, sql: str, parameters: object = None):
    cursor = _execute(connection, sql, parameters)
    try:
        return cursor.fetchone()
    finally:
        _close_cursor(cursor)


def _fetchall(connection: Any, sql: str, parameters: object = None):
    cursor = _execute(connection, sql, parameters)
    try:
        return cursor.fetchall()
    finally:
        _close_cursor(cursor)


def _run(connection: Any, sql: str, parameters: object = None) -> None:
    cursor = _execute(connection, sql, parameters)
    _close_cursor(cursor)


def _safe_rollback(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        pass


def _safe_close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _default_connection_error(error: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))


def _open(factory: ConnectionFactory):
    try:
        return factory()
    except Exception:
        raise MigrationError("PostgreSQL connection failed") from None


def _acquire_lock(connection: Any) -> None:
    try:
        _fetchone(
            connection,
            "SELECT pg_advisory_lock(%s)",
            (ADVISORY_LOCK_KEY,),
        )
    except Exception:
        _safe_rollback(connection)
        raise MigrationError("migration advisory lock could not be acquired") from None


def _release_lock(connection: Any) -> None:
    try:
        _fetchone(
            connection,
            "SELECT pg_advisory_unlock(%s)",
            (ADVISORY_LOCK_KEY,),
        )
        connection.commit()
    except Exception:
        _safe_rollback(connection)


def _history(connection: Any) -> dict[int, tuple[str, str]]:
    exists = _fetchone(
        connection,
        "SELECT to_regclass(%s)",
        (HISTORY_TABLE,),
    )
    if not exists or exists[0] is None:
        return {}
    rows = _fetchall(
        connection,
        f"SELECT version, filename, sha256 FROM {HISTORY_TABLE} ORDER BY version",
    )
    history: dict[int, tuple[str, str]] = {}
    for row in rows:
        version = int(row[0])
        if version in history:
            raise MigrationError("migration history contains duplicate versions")
        history[version] = (str(row[1]), str(row[2]).strip())
    return history


def _validate_history(
    migrations: tuple[Migration, ...], history: dict[int, tuple[str, str]]
) -> None:
    versions = sorted(history)
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("migration history versions are not continuous")
    local = {migration.version: migration for migration in migrations}
    for version, (filename, digest) in history.items():
        migration = local.get(version)
        if migration is None:
            raise MigrationError("database migration history is newer than this package")
        if migration.filename != filename or migration.sha256 != digest:
            raise MigrationError(
                f"applied migration filename or SHA-256 changed: {migration.filename}"
            )


def _apply(connection: Any, migration: Migration) -> None:
    try:
        _run(connection, migration.sql)
        _run(
            connection,
            f"INSERT INTO {HISTORY_TABLE} "
            "(version, filename, sha256, applied_at) "
            "VALUES (%s, %s, %s, CURRENT_TIMESTAMP)",
            (migration.version, migration.filename, migration.sha256),
        )
    except Exception:
        _safe_rollback(connection)
        raise MigrationError(f"migration application failed: {migration.filename}") from None


def run_migrations(
    config: PostgresConfig | None = None,
    *,
    migrations_directory: str | Path = MIGRATIONS_DIRECTORY,
    connection_factory: ConnectionFactory | None = None,
    is_connection_error: ConnectionErrorPredicate | None = None,
) -> MigrationReport:
    """Apply pending files while holding one session advisory lock.

    An injected zero-argument ``connection_factory`` avoids environment and
    driver access, allowing the runner to be tested without a live database.
    """

    migrations = discover_migrations(migrations_directory)
    if connection_factory is None:
        selected = config or PostgresConfig.from_env()
        connection_factory = lambda: connect(selected)
    connection_error = is_connection_error or _default_connection_error

    connection = _open(connection_factory)
    locked = False
    applied_files: list[str] = []
    already_files: list[str] = []
    recovered_versions: set[int] = set()
    try:
        _acquire_lock(connection)
        locked = True
        try:
            applied_history = _history(connection)
            _validate_history(migrations, applied_history)
        except MigrationError:
            raise
        except Exception:
            _safe_rollback(connection)
            raise MigrationError("migration history could not be read") from None

        initially_applied = frozenset(applied_history)
        index = 0
        while index < len(migrations):
            migration = migrations[index]
            recorded = applied_history.get(migration.version)
            if recorded is not None:
                if migration.version in initially_applied:
                    already_files.append(migration.filename)
                elif migration.filename not in applied_files:
                    applied_files.append(migration.filename)
                index += 1
                continue

            _apply(connection, migration)
            try:
                connection.commit()
            except Exception as error:
                if not connection_error(error):
                    _safe_rollback(connection)
                    raise MigrationError(
                        f"migration commit failed: {migration.filename}"
                    ) from None

                _safe_close(connection)
                locked = False
                try:
                    connection = _open(connection_factory)
                    _acquire_lock(connection)
                    locked = True
                    applied_history = _history(connection)
                    _validate_history(migrations, applied_history)
                except MigrationError:
                    raise MigrationCommitUnknown(
                        f"migration commit result is unknown: {migration.filename}"
                    ) from None
                except Exception:
                    raise MigrationCommitUnknown(
                        f"migration commit result is unknown: {migration.filename}"
                    ) from None

                recorded = applied_history.get(migration.version)
                if recorded is not None:
                    applied_files.append(migration.filename)
                    index += 1
                    continue
                if migration.version in recovered_versions:
                    raise MigrationCommitUnknown(
                        f"migration commit result is unknown: {migration.filename}"
                    )
                recovered_versions.add(migration.version)
                continue

            applied_history[migration.version] = (
                migration.filename,
                migration.sha256,
            )
            applied_files.append(migration.filename)
            index += 1

        return MigrationReport(
            tuple(migration.filename for migration in migrations),
            tuple(applied_files),
            tuple(already_files),
        )
    finally:
        if locked:
            _release_lock(connection)
        _safe_close(connection)


def main() -> int:
    try:
        report = run_migrations()
    except (
        MigrationError,
        PostgresConfigurationError,
        PostgresDependencyError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        f"migrations discovered={len(report.discovered)} "
        f"applied={len(report.applied)} "
        f"already_applied={len(report.already_applied)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
