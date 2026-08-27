"""Chunk creation transaction boundary and idempotent replay."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol

from src.shared import ObjectStorage, PublicError, StorageLocation

from .core import Chunk, ChunkingConfig, ParsedSection, chunk_sections, parse_document


@dataclass(frozen=True, slots=True)
class ChunkClaim:
    job_id: int
    attempt_id: int
    version_id: int
    worker_id: int
    claim_token: str


@dataclass(frozen=True, slots=True)
class ChunkPreparation:
    document_type: str
    location: StorageLocation
    replay: tuple[Chunk, ...] | None = None


@dataclass(frozen=True, slots=True)
class ChunkCreationResult:
    status: int
    chunks: tuple[Chunk, ...]


class ChunkStatePort(Protocol):
    def prepare(self, claim: ChunkClaim) -> ChunkPreparation: ...

    def commit(self, claim: ChunkClaim, chunks: tuple[Chunk, ...]) -> None: ...


@dataclass(slots=True)
class ChunkVersionState:
    job_id: int
    attempt_id: int
    version_id: int
    worker_id: int
    claim_token: str
    document_type: str
    location: StorageLocation
    status: str = "UPLOADED"
    chunks: tuple[Chunk, ...] = ()
    events: list[str] = field(default_factory=list)
    job_status: str = "PROCESSING"
    attempt_status: str = "STARTED"


class MemoryChunkState:
    """Small atomic ledger used by the stdlib runtime and tests."""

    def __init__(self, row: ChunkVersionState) -> None:
        self.row = row
        self._lock = RLock()

    def _owned(self, claim: ChunkClaim) -> bool:
        row = self.row
        return (
            claim.job_id == row.job_id
            and claim.attempt_id == row.attempt_id
            and claim.version_id == row.version_id
            and claim.worker_id == row.worker_id
            and claim.claim_token == row.claim_token
            and row.job_status == "PROCESSING"
            and row.attempt_status == "STARTED"
        )

    @staticmethod
    def _valid_chunks(chunks: tuple[Chunk, ...]) -> bool:
        return bool(chunks) and all(
            chunk.index == index
            and chunk.start >= 0
            and chunk.end > chunk.start
            and chunk.end - chunk.start == len(chunk.text)
            and chunk.text_sha256 == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            for index, chunk in enumerate(chunks)
        )

    def prepare(self, claim: ChunkClaim) -> ChunkPreparation:
        with self._lock:
            if not self._owned(claim):
                raise PublicError("EMBEDDING-JOB-003")
            if self.row.status == "CHUNKED":
                if not self._valid_chunks(self.row.chunks):
                    raise PublicError("DOCUMENT-CHUNK-001")
                return ChunkPreparation(
                    self.row.document_type, self.row.location, self.row.chunks
                )
            if self.row.status not in {"UPLOADED", "PARSING"}:
                raise PublicError("DOCUMENT-VERSION-005")
            if self.row.status == "UPLOADED":
                self.row.status = "PARSING"
                self.row.events.append("PARSE_STARTED")
            return ChunkPreparation(self.row.document_type, self.row.location)

    def commit(self, claim: ChunkClaim, chunks: tuple[Chunk, ...]) -> None:
        with self._lock:
            if not self._owned(claim):
                raise PublicError("EMBEDDING-JOB-003")
            if self.row.status != "PARSING":
                raise PublicError("DOCUMENT-VERSION-005")
            if not chunks:
                raise PublicError("DOCUMENT-PARSING-002")
            self.row.chunks = chunks
            self.row.status = "CHUNKED"
            self.row.events.append("CHUNKED")


class ChunkCreator:
    def __init__(
        self,
        storage: ObjectStorage,
        state: ChunkStatePort,
        *,
        chunk_size: int = 1000,
        overlap: int = 200,
        parser: Callable[[bytes, str], tuple[ParsedSection, ...]] = parse_document,
        chunker: Callable[
            [tuple[ParsedSection, ...], int, int], tuple[Chunk, ...]
        ] = chunk_sections,
    ) -> None:
        self.storage = storage
        self.state = state
        self.config = ChunkingConfig(chunk_size, overlap)
        self.parser = parser
        self.chunker = chunker

    def create(self, claim: ChunkClaim) -> ChunkCreationResult:
        prepared = self.state.prepare(claim)
        if prepared.replay is not None:
            return ChunkCreationResult(200, prepared.replay)

        data = self.storage.get(prepared.location)
        sections = self.parser(data, prepared.document_type)
        chunks = self.chunker(
            sections,
            self.config.chunk_size,
            self.config.overlap,
        )
        if not chunks:
            raise PublicError("DOCUMENT-PARSING-002")
        self.state.commit(claim, chunks)
        return ChunkCreationResult(201, chunks)
