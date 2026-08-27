from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.infra.postgres.config import (
    PostgresConfig,
    PostgresConfigurationError,
)
from src.infra.postgres.migrate import (
    HISTORY_TABLE,
    MIGRATIONS_DIRECTORY,
    MigrationError,
    discover_migrations,
    run_migrations,
)


class _DisconnectAfterCommit(RuntimeError):
    pass


class _Database:
    def __init__(self, *, disconnect_after_commit: bool = False) -> None:
        self.history_exists = False
        self.history: dict[int, tuple[str, str]] = {}
        self.disconnect_after_commit = disconnect_after_commit
        self.disconnected = False
        self.connections: list[_Connection] = []

    def connect(self) -> "_Connection":
        connection = _Connection(self)
        self.connections.append(connection)
        return connection


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.pending_history_exists = False
        self.pending_history: dict[int, tuple[str, str]] = {}
        self.closed = False

    def cursor(self) -> "_Cursor":
        return _Cursor(self)

    def commit(self) -> None:
        had_history_write = bool(self.pending_history)
        self.database.history_exists |= self.pending_history_exists
        self.database.history.update(self.pending_history)
        self.pending_history_exists = False
        self.pending_history.clear()
        if (
            had_history_write
            and self.database.disconnect_after_commit
            and not self.database.disconnected
        ):
            self.database.disconnected = True
            raise _DisconnectAfterCommit("simulated disconnect")

    def rollback(self) -> None:
        self.pending_history_exists = False
        self.pending_history.clear()

    def close(self) -> None:
        self.closed = True


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, parameters: object = None) -> None:
        normalized = " ".join(sql.lower().split())
        if "pg_advisory_lock" in normalized:
            self.rows = [(True,)]
        elif "pg_advisory_unlock" in normalized:
            self.rows = [(True,)]
        elif normalized.startswith("select to_regclass"):
            exists = (
                self.connection.database.history_exists
                or self.connection.pending_history_exists
            )
            self.rows = [(HISTORY_TABLE if exists else None,)]
        elif normalized.startswith(
            f"select version, filename, sha256 from {HISTORY_TABLE}"
        ):
            combined = dict(self.connection.database.history)
            combined.update(self.connection.pending_history)
            self.rows = [
                (version, filename, digest)
                for version, (filename, digest) in sorted(combined.items())
            ]
        elif normalized.startswith(f"insert into {HISTORY_TABLE}"):
            if not isinstance(parameters, tuple) or len(parameters) != 3:
                raise AssertionError("migration history parameters are malformed")
            version, filename, digest = parameters
            self.connection.pending_history[int(version)] = (
                str(filename),
                str(digest),
            )
        else:
            if f"create table if not exists {HISTORY_TABLE}" in normalized:
                self.connection.pending_history_exists = True
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        pass


class MigrationIntegrationTests(unittest.TestCase):
    def test_IT_MIGRATION_001_discovers_0001_through_0010_with_continuous_versions_and_sha256(self):
        migrations = discover_migrations()
        expected_names = (
            "0001_bootstrap.sql",
            "0002_identity.sql",
            "0003_documents.sql",
            "0004_access.sql",
            "0005_indexing.sql",
            "0006_sync.sql",
            "0007_mcp_search_history.sql",
            "0008_embedding_model_registry.sql",
            "0009_vector_search_hnsw.sql",
            "0010_keyword_search_fts.sql",
        )
        self.assertEqual(expected_names, tuple(item.filename for item in migrations))
        self.assertEqual(list(range(1, 11)), [item.version for item in migrations])
        for migration in migrations:
            expected = hashlib.sha256(
                (MIGRATIONS_DIRECTORY / migration.filename).read_bytes()
            ).hexdigest()
            self.assertEqual(expected, migration.sha256)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "0003_gap.sql").write_text("SELECT 3;", encoding="utf-8")
            with self.assertRaisesRegex(MigrationError, "continuous from 0001"):
                discover_migrations(root)

    def test_IT_MIGRATION_002_first_run_applies_then_second_run_skips_recorded_files(self):
        database = _Database()
        first = run_migrations(connection_factory=database.connect)
        second = run_migrations(connection_factory=database.connect)

        self.assertEqual(first.discovered, first.applied)
        self.assertEqual((), first.already_applied)
        self.assertEqual((), second.applied)
        self.assertEqual(second.discovered, second.already_applied)
        self.assertEqual(set(range(1, 11)), set(database.history))

    def test_IT_MIGRATION_003_applied_sha256_mismatch_stops_before_reapplication(self):
        database = _Database()
        run_migrations(connection_factory=database.connect)
        filename, _ = database.history[3]
        database.history[3] = (filename, "0" * 64)

        with self.assertRaisesRegex(MigrationError, "SHA-256 changed"):
            run_migrations(connection_factory=database.connect)
        self.assertEqual(set(range(1, 11)), set(database.history))

    def test_IT_MIGRATION_004_configuration_redacts_values_and_reports_missing_variable_names_only(self):
        secret_values = {
            "DB_HOST": "private-db-host",
            "DB_PORT": "5432",
            "DB_NAME": "private-db-name",
            "DB_USER": "private-db-user",
            "DB_PASSWORD": "private-db-password",
        }
        config = PostgresConfig.from_env(secret_values)
        if repr(config) != "PostgresConfig(<redacted>)":
            self.fail("PostgreSQL configuration representation was not redacted")

        missing_password = dict(secret_values)
        missing_password.pop("DB_PASSWORD")
        try:
            PostgresConfig.from_env(missing_password)
        except PostgresConfigurationError as error:
            message = str(error)
        else:
            self.fail("missing PostgreSQL configuration was accepted")
        if "DB_PASSWORD" not in message:
            self.fail("the missing environment variable name was not reported")
        if any(value in message for value in secret_values.values()):
            self.fail("a PostgreSQL configuration error exposed a secret value")

        def rejected_factory():
            raise RuntimeError(secret_values["DB_PASSWORD"])

        try:
            run_migrations(connection_factory=rejected_factory)
        except MigrationError as error:
            connection_message = str(error)
        else:
            self.fail("a rejected fake connection did not stop migration startup")
        if any(value in connection_message for value in secret_values.values()):
            self.fail("a migration connection error exposed a secret value")

    def test_IT_STAGE16_product_postgres_config_does_not_load_root_dotenv(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(
                PostgresConfigurationError,
                "DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD",
            ):
                PostgresConfig.from_env()

    def test_IT_STAGE16_postgres_connection_attempt_is_bounded_for_recovery(self):
        config = PostgresConfig("host", 5432, "database", "user", "password")
        self.assertEqual(5, config.connect_kwargs()["connect_timeout"])

    def test_IT_STAGE16_established_connection_detects_a_vanished_primary(self):
        kwargs = PostgresConfig("host", 5432, "database", "user", "password").connect_kwargs()

        self.assertEqual(1, kwargs["keepalives"])
        self.assertEqual(5, kwargs["keepalives_idle"])
        self.assertEqual(2, kwargs["keepalives_interval"])
        self.assertEqual(3, kwargs["keepalives_count"])
        self.assertEqual(15_000, kwargs["tcp_user_timeout"])
        # Windows ignores keepalives_count and picks its own retry count, so the
        # bound has to hold for a far larger number of probes than we configure.
        for retries in (kwargs["keepalives_count"], 10):
            bound = kwargs["keepalives_idle"] + kwargs["keepalives_interval"] * retries
            self.assertLess(
                bound, 30, "dead-peer detection must beat the worker dead threshold"
            )
        self.assertNotIn(
            "options",
            kwargs,
            "no statement_timeout: migration 0009 builds an HNSW index of unbounded duration",
        )

    def test_IT_MIGRATION_005_commit_disconnect_uses_reconnected_history_as_applied_result(self):
        database = _Database(disconnect_after_commit=True)
        report = run_migrations(
            connection_factory=database.connect,
            is_connection_error=lambda error: isinstance(
                error, _DisconnectAfterCommit
            ),
        )

        self.assertTrue(database.disconnected)
        self.assertGreaterEqual(len(database.connections), 2)
        self.assertEqual(report.discovered, report.applied)
        self.assertEqual(set(range(1, 11)), set(database.history))

    def test_IT_MIGRATION_006_schema_keeps_gapless_counters_and_deferred_ann_choice(self):
        bootstrap = (MIGRATIONS_DIRECTORY / "0001_bootstrap.sql").read_text(
            encoding="utf-8"
        )
        documents = (MIGRATIONS_DIRECTORY / "0003_documents.sql").read_text(
            encoding="utf-8"
        )
        indexing = (MIGRATIONS_DIRECTORY / "0005_indexing.sql").read_text(
            encoding="utf-8"
        )
        sync = (MIGRATIONS_DIRECTORY / "0006_sync.sql").read_text(
            encoding="utf-8"
        )
        registry = (MIGRATIONS_DIRECTORY / "0008_embedding_model_registry.sql").read_text(
            encoding="utf-8"
        )
        search = (MIGRATIONS_DIRECTORY / "0009_vector_search_hnsw.sql").read_text(
            encoding="utf-8"
        )
        keyword = (MIGRATIONS_DIRECTORY / "0010_keyword_search_fts.sql").read_text(
            encoding="utf-8"
        )

        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", bootstrap)
        self.assertIn("version_no integer NOT NULL", documents)
        self.assertIn("DEFERRABLE INITIALLY DEFERRED", documents)
        self.assertIn("attempt_no integer NOT NULL", indexing)
        self.assertIn("embedding vector NOT NULL", indexing)
        self.assertNotIn("embedding vector(", indexing)
        self.assertNotIn("USING hnsw", indexing)
        self.assertNotIn("USING ivfflat", indexing)
        self.assertIn("attempt_no integer NOT NULL", sync)
        self.assertIn("RENAME COLUMN name TO model_name", registry)
        self.assertIn("ADD COLUMN provider", registry)
        self.assertIn("ADD COLUMN model_version", registry)
        self.assertNotIn("INSERT", registry.upper())
        self.assertIn("USING hnsw", search)
        self.assertIn("(embedding::vector(1536)) vector_cosine_ops", search)
        self.assertIn("embedding_model_id = %s", search)
        self.assertIn("status = ''ACTIVE''", search)
        self.assertNotIn("vector_l2_ops", search)
        self.assertNotIn("vector_ip_ops", search)
        self.assertIn(
            "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED", keyword
        )
        self.assertIn("USING gin (search_vector)", keyword)
        self.assertIn("token_estimate integer NOT NULL", indexing)
        self.assertNotIn("ADD COLUMN search_length", keyword)

        counter_lines = "\n".join(
            line
            for sql in (documents, indexing, sync)
            for line in sql.splitlines()
            if "version_no" in line or "attempt_no" in line
        ).casefold()
        self.assertNotIn("serial", counter_lines)
        self.assertNotIn("identity", counter_lines)
        self.assertNotIn("sequence", counter_lines)


if __name__ == "__main__":
    unittest.main()
