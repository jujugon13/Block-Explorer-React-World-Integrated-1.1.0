"""Small immutable results shared by guardrail operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuestionClassification:
    kind: str
    confidence: float
    indicator_count: int
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class InjectionResult:
    blocked: bool
    reason: str | None
    score: float
    distinct_keywords: tuple[str, ...]
    judge_called: bool
    suspicious_base64: bool


@dataclass(frozen=True, slots=True)
class RetrievalGateResult:
    passed: bool
    soft_failed: bool
    terminal_answer: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class PIIMaskingResult:
    results: list[dict[str, object]]
    detections: int


@dataclass(frozen=True, slots=True)
class GenerationResult:
    answer: str
    branch: str
    fallback: str | None = None
    evidence_attempted: bool = False
    rescued: bool = False


@dataclass(frozen=True, slots=True)
class NumericVerification:
    passed: bool
    expressions: tuple[str, ...]
    unsupported: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnswerGuardrailResult:
    answer: str
    passed: bool
    score: float
    parse_failed: bool

