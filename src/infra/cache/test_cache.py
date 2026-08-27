from __future__ import annotations

import sys
import unittest
from types import ModuleType
from unittest.mock import patch

from src.infra.cache import (
    REDIS_TIMEOUT_SECONDS,
    RedisCache,
    RedisConfigurationError,
    RedisConfig,
    RedisDependencyError,
    build_redis_cache,
)


class _Client:
    def __init__(self) -> None:
        self.values: dict[str, str | bytes] = {}
        self.set_calls: list[tuple[str, str, int]] = []
        self.deleted: list[str] = []
        self.error: Exception | None = None

    def get(self, key: str) -> str | bytes | None:
        self._raise_if_needed()
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> bool:
        self._raise_if_needed()
        self.values[key] = value
        self.set_calls.append((key, value, ex))
        return True

    def delete(self, key: str) -> int:
        self._raise_if_needed()
        self.values.pop(key, None)
        self.deleted.append(key)
        return 1

    def _raise_if_needed(self) -> None:
        if self.error is not None:
            raise self.error


class _RedisFactory:
    calls: list[tuple[str, dict[str, object]]] = []

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> _Client:
        cls.calls.append((url, kwargs))
        return _Client()


class RedisCacheTests(unittest.TestCase):
    def test_IT_CACHE_001_config_requires_valid_url_and_redacts_secret(self):
        config = RedisConfig.from_env({"REDIS_URL": "rediss://:secret@cache.example/0"})

        self.assertEqual("rediss://:secret@cache.example/0", config.url)
        self.assertEqual("RedisConfig(<redacted>)", repr(config))
        for value in ("", "https://cache.example", "redis://"):
            with self.subTest(value=value), self.assertRaises(RedisConfigurationError):
                RedisConfig.from_env({"REDIS_URL": value})

    def test_IT_CACHE_002_get_set_delete_match_shared_cache_contract(self):
        client = _Client()
        cache = RedisCache(client)

        self.assertIsNone(cache.get("missing"))
        cache.set("search:key", "value", 3600)
        self.assertEqual("value", cache.get("search:key"))
        client.values["legacy"] = b"decoded"
        self.assertEqual("decoded", cache.get("legacy"))
        cache.delete("search:key")
        self.assertIsNone(cache.get("search:key"))
        self.assertEqual([("search:key", "value", 3600)], client.set_calls)
        self.assertEqual(["search:key"], client.deleted)

    def test_IT_CACHE_003_operational_failures_are_fail_open_connection_errors(self):
        client = _Client()
        client.error = RuntimeError("redis://user:secret@host detail")
        cache = RedisCache(client)

        for call in (
            lambda: cache.get("key"),
            lambda: cache.set("key", "value", 1),
            lambda: cache.delete("key"),
        ):
            with self.subTest(call=call), self.assertRaisesRegex(
                ConnectionError, "cache unavailable"
            ) as raised:
                call()
            self.assertNotIn("secret", str(raised.exception))

    def test_IT_CACHE_004_builder_imports_redis_lazily_with_fixed_timeouts(self):
        _RedisFactory.calls.clear()
        redis_module = ModuleType("redis")
        redis_module.Redis = _RedisFactory
        with patch.dict(sys.modules, {"redis": redis_module}):
            cache = build_redis_cache(RedisConfig("redis://cache.example/0"))

        self.assertIsInstance(cache, RedisCache)
        self.assertEqual(
            [("redis://cache.example/0", {
                "decode_responses": True,
                "socket_connect_timeout": REDIS_TIMEOUT_SECONDS,
                "socket_timeout": REDIS_TIMEOUT_SECONDS,
            })],
            _RedisFactory.calls,
        )

    def test_IT_CACHE_005_builder_reports_missing_optional_dependency(self):
        with patch.dict(sys.modules, {"redis": None}):
            with self.assertRaisesRegex(RedisDependencyError, "redis is required"):
                build_redis_cache(RedisConfig("redis://cache.example/0"))


if __name__ == "__main__":
    unittest.main()
