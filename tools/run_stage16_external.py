#!/usr/bin/env python3
"""Load root .env into one stage-16 subprocess without exposing its values."""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOTENV = ROOT / ".env"
REQUIRED = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "S3_BUCKET",
)
STATIC_AWS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
WEB_IDENTITY_AWS = ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE")
CONTAINER_AWS = (
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)
REGIONS = ("AWS_REGION", "AWS_DEFAULT_REGION")
ALLOWED_ENVIRONMENTS = frozenset({"test", "staging"})
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _value(text: str) -> str:
    value = text.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            raise ValueError("invalid quoted .env value") from None
        if not isinstance(decoded, str):
            raise ValueError(".env values must be strings")
        return decoded
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def read_dotenv(path: Path = DOTENV) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, encoded = line.split("=", 1)
        name = name.strip()
        if not _KEY.fullmatch(name):
            raise ValueError(f"invalid .env key at line {number}")
        values[name] = _value(encoded)
    return values


def child_environment(
    parent: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str] | None = None,
) -> dict[str, str]:
    child = dict(os.environ if parent is None else parent)
    for name, value in (read_dotenv() if dotenv is None else dotenv).items():
        if not child.get(name):
            child[name] = value
    current = child.get("PYTHONPATH")
    child["PYTHONPATH"] = str(ROOT) + (os.pathsep + current if current else "")
    return child


def _present(environment: Mapping[str, str], names: tuple[str, ...]) -> bool:
    return all(bool(environment.get(name)) for name in names)


def preflight(environment: Mapping[str, str]) -> bool:
    missing = [name for name in REQUIRED if not environment.get(name)]
    for name in REQUIRED:
        print(f"ENV {name}={'PRESENT' if environment.get(name) else 'MISSING'}")

    credentials = (
        _present(environment, STATIC_AWS)
        or bool(environment.get("AWS_PROFILE"))
        or _present(environment, WEB_IDENTITY_AWS)
        or any(environment.get(name) for name in CONTAINER_AWS)
    )
    region = any(environment.get(name) for name in REGIONS)
    print(f"ENV AWS_CREDENTIALS={'PRESENT' if credentials else 'MISSING'}")
    print(f"ENV AWS_REGION={'PRESENT' if region else 'MISSING'}")
    print(
        "ENV STAGE16_TEST_ENVIRONMENT="
        + ("PRESENT" if environment.get("STAGE16_TEST_ENVIRONMENT") else "MISSING")
    )

    selected = environment.get("STAGE16_TEST_ENVIRONMENT", "").casefold()
    allowed = selected in ALLOWED_ENVIRONMENTS
    if not allowed:
        print("STAGE16_ENVIRONMENT=BLOCKED")
    if missing or not credentials or not region or not allowed:
        print("STAGE16_EXTERNAL_PREFLIGHT=BLOCKED")
    return not missing and credentials and region and allowed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    known, remaining = parser.parse_known_args(argv)
    try:
        environment = child_environment()
    except (OSError, ValueError):
        print("STAGE16_DOTENV=BLOCKED")
        return 2
    if not preflight(environment):
        return 2
    if known.check_only:
        print("STAGE16_EXTERNAL_PREFLIGHT=PASS")
        return 0
    command = [sys.executable, str(ROOT / "tools" / "stage16_e2e.py")]
    command.extend(remaining or ("--scenario", "all"))
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
