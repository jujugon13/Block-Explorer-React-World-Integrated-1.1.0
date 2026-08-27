"""Redis-compatible implementation of ``src.shared.CacheStore``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from src.shared import CacheStore


REDIS_URL_ENVIRONMENT_KEY = "REDIS_URL"
REDIS_TIMEOUT_SECONDS = 1.0
_SUPPORTED_SCHEMES = frozenset({"redis", "rediss", "unix"})


class RedisConfigurationError(ValueError):
    """Required Redis deployment configuration is absent or malformed."""


class RedisDependencyError(RuntimeError):
    """The optional Redis client dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class RedisConfig:
    url: str = field(repr=False)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "RedisConfig":
        source = os.environ if environment is None else environment
        url = source.get(REDIS_URL_ENVIRONMENT_KEY, "").strip()
        parsed = urlparse(url)
        valid_network_url = parsed.scheme in {"redis", "rediss"} and bool(parsed.hostname)
        valid_unix_url = parsed.scheme == "unix" and bool(parsed.path)
        if not url:
            raise RedisConfigurationError("REDIS_URL is required")
        if parsed.scheme not in _SUPPORTED_SCHEMES or not (
            valid_network_url or valid_unix_url
        ):
            raise RedisConfigurationError("REDIS_URL is invalid")
        return cls(url)

    def __repr__(self) -> str:
        return "RedisConfig(<redacted>)"


class RedisCache(CacheStore):
    """Thin synchronous cache adapter with the fixed one-second client timeout."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def get(self, key: str) -> str | None:
        try:
            value = self.client.get(key)
        except Exception:
            raise ConnectionError("cache unavailable") from None
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, bytes):
            try:
                return value.decode()
            except UnicodeDecodeError:
                pass
        raise ConnectionError("cache unavailable")

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            self.client.set(key, value, ex=ttl_seconds)
        except Exception:
            raise ConnectionError("cache unavailable") from None

    def delete(self, key: str) -> None:
        try:
            self.client.delete(key)
        except Exception:
            raise ConnectionError("cache unavailable") from None


def _default_client(config: RedisConfig) -> Any:
    try:
        from redis import Redis
    except ImportError:
        raise RedisDependencyError("redis is required for Redis cache") from None
    try:
        return Redis.from_url(
            config.url,
            decode_responses=True,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
        )
    except Exception:
        raise RedisConfigurationError("REDIS_URL is invalid") from None


def build_redis_cache(
    config: RedisConfig | None = None,
    *,
    client: Any | None = None,
) -> CacheStore:
    """Build the shared cache port without contacting Redis during startup."""

    selected = config if config is not None else RedisConfig.from_env()
    return RedisCache(client if client is not None else _default_client(selected))
