from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.infra.settings import SettingsConfigurationError, load_settings


class SettingsLoaderTests(unittest.TestCase):
    @staticmethod
    def _environment(**overrides: str) -> dict[str, str]:
        return {
            "VECTORSHELF_JWT__SECRET": "test-secret",
            **overrides,
        }

    def test_FR_SYS_006_defaults_include_system_and_S14_values(self) -> None:
        settings = load_settings(Path("does-not-exist.toml"), self._environment())

        self.assertEqual(8080, settings["server.port"])
        self.assertEqual("local", settings["storage.type"])
        self.assertEqual(1000, settings["document.chunking.chunk-size"])
        self.assertFalse(settings["indexing.worker.enabled"])
        self.assertFalse(settings["sync.dispatcher.enabled"])
        self.assertFalse(settings["sync.reconciliation.enabled"])
        self.assertEqual("hybrid", settings["search_mode"])
        self.assertTrue(settings["cache_enabled"])

    def test_FR_SYS_006_toml_profile_then_environment_precedence(self) -> None:
        contents = """
[settings]
search_mode = "keyword"

[settings.server]
port = 8081

[profiles.staging]
search_mode = "vector"

[profiles.staging.document.chunking]
chunk-size = 1200
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vectorshelf.toml"
            path.write_text(contents, encoding="utf-8")
            settings = load_settings(
                path,
                self._environment(
                    VECTORSHELF_PROFILE="staging",
                    VECTORSHELF_SERVER__PORT="8082",
                    VECTORSHELF_DOCUMENT__CHUNKING__CHUNK_SIZE="1300",
                    VECTORSHELF_SEARCH_MODE="cascading",
                ),
            )

        self.assertEqual(8082, settings["server.port"])
        self.assertEqual(1300, settings["document.chunking.chunk-size"])
        self.assertEqual("cascading", settings["search_mode"])

    def test_FR_SYS_007_rejects_unknown_bad_type_and_bad_port(self) -> None:
        cases = (
            {"VECTORSHELF_UNKNOWN__VALUE": "1"},
            {"VECTORSHELF_INDEXING__WORKER__ENABLED": "yes"},
            {"VECTORSHELF_SERVER__PORT": "65536"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(SettingsConfigurationError):
                    load_settings(Path("does-not-exist.toml"), self._environment(**overrides))

    def test_FR_SYS_007_requires_jwt_secret(self) -> None:
        with self.assertRaisesRegex(SettingsConfigurationError, "jwt.secret is required"):
            load_settings(Path("does-not-exist.toml"), {})

    def test_FR_SYS_007_s3_bucket_uses_the_decided_provider_environment_key(self) -> None:
        settings = load_settings(
            Path("does-not-exist.toml"),
            self._environment(
                VECTORSHELF_STORAGE__TYPE="s3",
                S3_BUCKET="vectorshelf-docs-apne2",
            ),
        )

        self.assertEqual("vectorshelf-docs-apne2", settings["storage.bucket"])
        with self.assertRaisesRegex(SettingsConfigurationError, "conflicts"):
            load_settings(
                Path("does-not-exist.toml"),
                self._environment(
                    VECTORSHELF_STORAGE__TYPE="s3",
                    VECTORSHELF_STORAGE__BUCKET="one",
                    S3_BUCKET="two",
                ),
            )

    def test_D13_dotenv_populates_missing_process_settings_without_overriding_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text(
                "VECTORSHELF_JWT__SECRET=dotenv-secret\n"
                "VECTORSHELF_SERVER__PORT=8081\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(
                    os.environ,
                    {"VECTORSHELF_SERVER__PORT": "8082"},
                    clear=True,
                ):
                    settings = load_settings()
                    self.assertEqual("dotenv-secret", os.environ["VECTORSHELF_JWT__SECRET"])
                    self.assertEqual(8082, settings["server.port"])
            finally:
                os.chdir(previous)
