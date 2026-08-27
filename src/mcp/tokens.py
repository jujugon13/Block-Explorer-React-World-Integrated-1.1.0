"""Long-lived API-key issue, storage, revocation, and authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from src.shared import McpTokenRecord, McpTokenStore, Principal, PublicError

MCP_KEY_PREFIX = "vectorshelf_mcp_"
MCP_PRINCIPAL_SUBJECT = "vectorshelf-mcp-api-key"
_KEY_PATTERN = re.compile(r"^vectorshelf_mcp_[A-Za-z0-9_-]{43}$")


class InMemoryMcpTokenStore:
    """Deterministic relational-store stand-in used without a DB driver."""

    def __init__(self) -> None:
        self._records: dict[str, McpTokenRecord] = {}
        self._ids_by_hash: dict[str, str] = {}
        self._lock = RLock()

    def insert(self, record: McpTokenRecord) -> None:
        with self._lock:
            if record.token_id in self._records or record.key_sha256 in self._ids_by_hash:
                raise ValueError("duplicate MCP token")
            self._records[record.token_id] = record
            self._ids_by_hash[record.key_sha256] = record.token_id

    def get(self, token_id: str) -> McpTokenRecord | None:
        with self._lock:
            return self._records.get(token_id)

    def find_by_hash(self, key_sha256: str) -> McpTokenRecord | None:
        with self._lock:
            token_id = self._ids_by_hash.get(key_sha256)
            return self._records.get(token_id) if token_id is not None else None

    def list_for_owner(self, owner_user_id: int) -> Sequence[McpTokenRecord]:
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.owner_user_id == owner_user_id
            )

    def update(self, record: McpTokenRecord) -> McpTokenRecord:
        with self._lock:
            if record.token_id not in self._records:
                raise KeyError(record.token_id)
            self._records[record.token_id] = record
            return record

    def touch_last_used_if_active(
        self, token_id: str, key_sha256: str, used_at: datetime
    ) -> bool:
        with self._lock:
            record = self._records.get(token_id)
            if (
                record is None
                or record.key_sha256 != key_sha256
                or record.revoked_at is not None
            ):
                return False
            self._records[token_id] = replace(record, last_used_at=used_at)
            return True

    @property
    def records(self) -> tuple[McpTokenRecord, ...]:
        with self._lock:
            return tuple(self._records.values())


class McpTokenService:
    def __init__(
        self,
        store: McpTokenStore,
        *,
        clock: Callable[[], datetime] | None = None,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        token_id: Callable[[], str] = lambda: str(uuid4()),
        principal_factory: Callable[[int], Principal | None] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_bytes = random_bytes
        self._token_id = token_id
        self._principal_factory = principal_factory
        self._lock = RLock()

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _owner(principal: Principal | None) -> int:
        if principal is None or principal.user_id is None:
            raise PublicError("COMMON-007")
        return principal.user_id

    @staticmethod
    def _hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def _payload(record: McpTokenRecord) -> dict[str, object]:
        return {
            "tokenId": record.token_id,
            "createdAt": record.created_at.isoformat(),
            "lastUsedAt": record.last_used_at.isoformat() if record.last_used_at else None,
            "revokedAt": record.revoked_at.isoformat() if record.revoked_at else None,
        }

    def issue(self, principal: Principal | None) -> dict[str, object]:
        owner_user_id = self._owner(principal)
        entropy = self._random_bytes(32)
        if len(entropy) != 32:
            raise ValueError("MCP key entropy must be exactly 32 bytes")
        suffix = base64.urlsafe_b64encode(entropy).rstrip(b"=").decode("ascii")
        raw_key = MCP_KEY_PREFIX + suffix
        record = McpTokenRecord(
            self._token_id(), owner_user_id, self._hash(raw_key), self._now()
        )
        self.store.insert(record)
        return {**self._payload(record), "apiKey": raw_key}

    def list(self, principal: Principal | None) -> list[dict[str, object]]:
        owner_user_id = self._owner(principal)
        return [self._payload(record) for record in self.store.list_for_owner(owner_user_id)]

    def revoke(self, principal: Principal | None, token_id: str) -> dict[str, object]:
        owner_user_id = self._owner(principal)
        with self._lock:
            record = self.store.get(token_id)
            if record is None:
                raise PublicError("COMMON-003")
            if record.owner_user_id != owner_user_id:
                raise PublicError("ROLE-002")
            if record.revoked_at is None:
                record = replace(record, revoked_at=self._now())
                record = self.store.update(record)
            return self._payload(record)

    def authenticate(self, raw_key: str | None) -> Principal | None:
        if raw_key is None or _KEY_PATTERN.fullmatch(raw_key) is None:
            return None
        digest = self._hash(raw_key)
        with self._lock:
            record = self.store.find_by_hash(digest)
            if (
                record is None
                or record.revoked_at is not None
                or not hmac.compare_digest(record.key_sha256, digest)
            ):
                return None
        loaded = (
            self._principal_factory(record.owner_user_id)
            if self._principal_factory is not None
            else None
        )
        if self._principal_factory is not None and loaded is None:
            return None
        with self._lock:
            if not self.store.touch_last_used_if_active(
                record.token_id, digest, self._now()
            ):
                return None
        loaded = loaded or Principal(MCP_PRINCIPAL_SUBJECT, user_id=record.owner_user_id)
        return replace(
            loaded,
            subject=MCP_PRINCIPAL_SUBJECT,
            user_id=record.owner_user_id,
        )

    def authenticate_header(self, authorization: str | None) -> Principal | None:
        if authorization is None or not authorization.startswith("Bearer "):
            return None
        return self.authenticate(authorization[7:])
