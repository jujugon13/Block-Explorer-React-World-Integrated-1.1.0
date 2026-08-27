"""Strict process settings from the optional TOML file and environment.

The application consumes a flat mapping, while TOML may use nested tables.
Environment variables use ``__`` for dots and ``_`` for hyphens, for example
``VECTORSHELF_INDEXING__WORKER__POLL_INTERVAL``.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path

from src.search.settings import DEFAULTS as SEARCH_DEFAULTS


class SettingsConfigurationError(ValueError):
    """The active VectorShelf settings are absent, unknown, or malformed."""


SYSTEM_DEFAULTS: dict[str, object] = {
    "server.port": 8080,
    "storage.type": "local",
    "storage.bucket": "",
    "storage.local.root": "./data/vectorshelf",
    "jwt.expiration": 3600,
    "document.chunking.chunk-size": 1000,
    "document.chunking.overlap": 200,
    "embedding.connect-timeout": 5.0,
    "embedding.response-timeout": 5.0,
    "embedding.document-batch-response-timeout": 30.0,
    "embedding.batch-size": 4,
    "embedding.character-budget": 4000,
    "embedding.token-budget": 900,
    "embedding.circuit-enabled": True,
    "embedding.circuit-failure-threshold": 5,
    "embedding.circuit-open-seconds": 30.0,
    "indexing.worker.enabled": False,
    "indexing.worker.name": "indexing-worker",
    "indexing.worker.heartbeat-interval": 10.0,
    "indexing.worker.dead-threshold": 30.0,
    "indexing.worker.poll-interval": 1.0,
    "indexing.worker.empty-poll-max": 10.0,
    "indexing.worker.max-concurrency": 2,
    "indexing.worker.lease-duration": 300.0,
    "indexing.worker.lease-renew-interval": 60.0,
    "indexing.worker.recovery-interval": 30.0,
    "indexing.worker.recovery-batch-size": 100,
    "indexing.worker.retry-initial": 10.0,
    "indexing.worker.retry-max": 300.0,
    "indexing.worker.retry-jitter": 0.2,
    "indexing.worker.shutdown-grace": 30.0,
    "indexing.job.max-retries": 3,
    "indexing.job.default-priority": 0,
    "sync.dispatcher.enabled": False,
    "sync.dispatcher.name": "sync-dispatcher",
    "sync.dispatcher.poll-interval": 1.0,
    "sync.dispatcher.lease-duration": 30.0,
    "sync.dispatcher.recovery-interval": 10.0,
    "sync.dispatcher.recovery-batch": 100,
    "sync.dispatcher.retry-initial": 5.0,
    "sync.dispatcher.retry-max": 60.0,
    "sync.dispatcher.max-retries": 5,
    "sync.reconciliation.enabled": False,
    "sync.reconciliation.mode": "DRY_RUN",
    "sync.reconciliation.batch": 100,
    "sync.reconciliation.period": 300.0,
    "sync.reconciliation.stalled-threshold": 900.0,
    "sync.reconciliation.orphan-grace": 86400.0,
    "dashboard.push-debounce": 0.3,
    "scheduler.background-worker-pool": 4,
}

DEFAULT_SETTINGS: dict[str, object] = {**SYSTEM_DEFAULTS, **SEARCH_DEFAULTS}
_REQUIRED_STRING_KEYS = frozenset({"jwt.secret"})
_KEY_TYPES = {
    **{key: type(value) for key, value in DEFAULT_SETTINGS.items()},
    **{key: str for key in _REQUIRED_STRING_KEYS},
}
_ENV_PREFIX = "VECTORSHELF_"
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+\Z")
_DOTENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# S14 permits these nested TOML tables while the existing search implementation
# consumes their corresponding flat settings.
_NESTED_ALIASES = {
    "injection.enabled": "injection_detection_enabled",
    "injection.action": "injection_action",
    "injection.block-message": "injection_block_message",
    "hallucination.enabled": "hallucination_detection_enabled",
    "hallucination.threshold": "hallucination_threshold",
    "hallucination.judge-model": "hallucination_judge_model",
    "retrieval-quality-gate.enabled": "retrieval_quality_gate_enabled",
    "retrieval-quality-gate.min-top-score": "min_top_score",
    "retrieval-quality-gate.min-doc-count": "min_doc_count",
    "retrieval-quality-gate.min-doc-score": "min_doc_score",
    "retrieval-quality-gate.soft-mode": "soft_mode",
    "retrieval-quality-gate.not-found-message": "not_found_message",
    "faithfulness.enabled": "faithfulness_enabled",
    "faithfulness.threshold": "faithfulness_threshold",
    "faithfulness.action": "faithfulness_action",
    "pii.enabled": "pii_detection_enabled",
}


def load_settings(
    path: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return defaults overlaid by TOML, selected profile, then environment.

    ``vectorshelf.toml`` is optional.  The selected profile is
    ``VECTORSHELF_PROFILE`` or ``local``; a missing selected profile simply has
    no profile-specific override.
    """

    if environment is None:
        _load_dotenv()
    source = os.environ if environment is None else environment
    profile = _profile(source)
    settings = dict(DEFAULT_SETTINGS)
    document = _read_toml(Path(path) if path is not None else Path("vectorshelf.toml"))
    _apply_toml_section(settings, document.get("settings", {}), "settings")
    profiles = document.get("profiles", {})
    if not isinstance(profiles, Mapping):
        raise SettingsConfigurationError("profiles must be a TOML table")
    selected = profiles.get(profile, {})
    if not isinstance(selected, Mapping):
        raise SettingsConfigurationError(f"profiles.{profile} must be a TOML table")
    _apply_toml_section(settings, selected, f"profiles.{profile}")
    _apply_environment(settings, source)
    _apply_provider_aliases(settings, source)
    _validate(settings)
    return settings


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load UTF-8 KEY=VALUE entries without replacing process settings."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as error:
        raise SettingsConfigurationError(f"unable to read .env settings file: {path}") from error
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _DOTENV_KEY.fullmatch(key):
            raise SettingsConfigurationError(f"invalid .env assignment at {path}:{number}")
        os.environ.setdefault(key, value.strip())


def _profile(environment: Mapping[str, str]) -> str:
    profile = environment.get("VECTORSHELF_PROFILE", "local")
    if not isinstance(profile, str) or not profile.strip():
        raise SettingsConfigurationError("VECTORSHELF_PROFILE must be non-blank")
    return profile.strip()


def _read_toml(path: Path) -> Mapping[str, object]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise SettingsConfigurationError(f"invalid TOML settings file: {path}") from error
    except OSError as error:
        raise SettingsConfigurationError(f"unable to read settings file: {path}") from error
    if not isinstance(document, Mapping):  # tomllib always returns dict; retain the boundary.
        raise SettingsConfigurationError("settings file must contain a TOML table")
    unknown = set(document) - {"settings", "profiles"}
    if unknown:
        raise SettingsConfigurationError(
            "unknown top-level TOML table: " + ", ".join(sorted(str(item) for item in unknown))
        )
    settings = document.get("settings", {})
    if not isinstance(settings, Mapping):
        raise SettingsConfigurationError("settings must be a TOML table")
    return document


def _apply_toml_section(
    target: dict[str, object], section: Mapping[str, object], label: str
) -> None:
    if not isinstance(section, Mapping):
        raise SettingsConfigurationError(f"{label} must be a TOML table")
    for raw_key, value in _flatten(section):
        key = _known_key(raw_key)
        target[key] = _coerce(key, value, f"TOML {label}.{raw_key}")


def _flatten(
    values: Mapping[str, object], prefix: str = ""
) -> Iterator[tuple[str, object]]:
    for raw_key, value in values.items():
        if not isinstance(raw_key, str):
            raise SettingsConfigurationError("TOML setting keys must be strings")
        key = f"{prefix}.{raw_key}" if prefix else raw_key
        if isinstance(value, Mapping):
            yield from _flatten(value, key)
        else:
            yield key, value


def _apply_environment(target: dict[str, object], environment: Mapping[str, str]) -> None:
    seen: dict[str, str] = {}
    for name, value in environment.items():
        if name == "VECTORSHELF_PROFILE" or not name.startswith(_ENV_PREFIX):
            continue
        if not isinstance(value, str):
            raise SettingsConfigurationError(f"{name} must be text")
        key = _environment_key(name)
        if key in seen:
            raise SettingsConfigurationError(
                f"{name} duplicates {seen[key]} for setting {key}"
            )
        seen[key] = name
        target[key] = _coerce(key, value, name)


def _apply_provider_aliases(
    settings: dict[str, object], environment: Mapping[str, str]
) -> None:
    """Keep D-9's standard S3 bucket variable aligned with the §7 setting."""

    if settings.get("storage.type") != "s3":
        return
    bucket = environment.get("S3_BUCKET", "").strip()
    configured = settings.get("storage.bucket", "")
    if not isinstance(configured, str):
        raise SettingsConfigurationError("storage.bucket must be text")
    if bucket and configured and bucket != configured:
        raise SettingsConfigurationError("S3_BUCKET conflicts with storage.bucket")
    if bucket:
        settings["storage.bucket"] = bucket


def _environment_key(name: str) -> str:
    suffix = name.removeprefix(_ENV_PREFIX)
    parts = suffix.lower().split("__")
    if not suffix or any(not part for part in parts):
        raise SettingsConfigurationError(f"invalid VectorShelf environment key: {name}")
    candidates = (
        ".".join(part.replace("_", "-") for part in parts),
        "_".join(parts),
    )
    for candidate in candidates:
        try:
            return _known_key(candidate)
        except SettingsConfigurationError:
            continue
    raise SettingsConfigurationError(f"unknown VectorShelf setting: {name}")


def _known_key(raw_key: str) -> str:
    candidates = (
        raw_key,
        raw_key.replace("_", "-"),
        raw_key.replace(".", "_").replace("-", "_"),
    )
    for candidate in candidates:
        key = _NESTED_ALIASES.get(candidate, candidate)
        if key in _KEY_TYPES:
            return key
    raise SettingsConfigurationError(f"unknown VectorShelf setting: {raw_key}")


def _coerce(key: str, value: object, source: str) -> object:
    expected = _KEY_TYPES[key]
    if isinstance(value, str):
        return _parse_text(key, value, source, expected)
    if expected is bool:
        if isinstance(value, bool):
            return value
    elif expected is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif expected is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value)
            if math.isfinite(result):
                return result
    elif expected is str and isinstance(value, str):
        return value
    raise SettingsConfigurationError(f"{source} must be {expected.__name__} for {key}")


def _parse_text(key: str, text: str, source: str, expected: type[object]) -> object:
    if expected is str:
        return text
    value = text.strip()
    if expected is bool:
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    elif expected is int:
        if _INTEGER_TEXT.fullmatch(value):
            return int(value)
    elif expected is float:
        try:
            parsed = float(value)
        except ValueError:
            parsed = None
        if parsed is not None and math.isfinite(parsed):
            return parsed
    raise SettingsConfigurationError(f"{source} must be {expected.__name__} for {key}")


def _validate(settings: Mapping[str, object]) -> None:
    port = settings["server.port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535:
        raise SettingsConfigurationError("server.port must be an integer from 1 to 65535")
    secret = settings.get("jwt.secret")
    if not isinstance(secret, str) or not secret.strip():
        raise SettingsConfigurationError("jwt.secret is required")
    storage_type = settings.get("storage.type")
    if storage_type not in {"local", "minio", "s3"}:
        raise SettingsConfigurationError("storage.type must be local, minio, or s3")
    if storage_type in {"minio", "s3"}:
        bucket = settings.get("storage.bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            raise SettingsConfigurationError("storage.bucket is required for external storage")
