"""Shared relational-driver boundary used by feature-owned adapters."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class CommitOutcomeUnknown(RuntimeError):
    """The connection failed after commit began, so its result is unknown."""


def open_sqlite_database(database: str | Path) -> Any:
    """Open the stdlib SQLite adapter with the process-wide safety defaults."""

    connection = sqlite3.connect(
        str(database),
        check_same_thread=False,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if str(database) != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    return connection
