"""Question classification and the three-layer injection detector."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Iterable, Sequence

from ..models import InjectionResult, QuestionClassification


# The pack omits the concrete lists. IMPLEMENTATION_DECISIONS.md makes both
# lists injectable and permits these conservative reference defaults.
DEFAULT_INJECTION_PATTERNS = (
    r"이전\s+지시(?:를|사항을)?\s+무시",
    r"위의?\s+지시(?:를|사항을)?\s+무시",
    r"규칙(?:을|를)\s+무시하고",
    r"시스템\s+메시지(?:를|을)\s+(?:보여|출력)",
    r"숨겨진\s+프롬프트(?:를|를)?\s+(?:보여|출력)",
    r"개발자\s+지시(?:를|를)?\s+(?:보여|출력)",
    r"ignore\s+(?:all\s+)?previous\s+instructions?",
    r"disregard\s+(?:all\s+)?previous\s+instructions?",
    r"reveal\s+(?:the\s+)?system\s+prompt",
    r"print\s+(?:the\s+)?hidden\s+prompt",
    r"override\s+(?:all\s+)?safety\s+rules?",
)

DEFAULT_RISK_KEYWORDS = (
    "무시",
    "시스템",
    "프롬프트",
    "지시",
    "규칙",
    "우회",
    "출력",
    "공개",
    "역할",
    "탈옥",
    "비밀",
    "명령",
    "덮어쓰기",
    "개발자",
    "ignore",
    "system",
    "prompt",
    "instruction",
    "override",
    "bypass",
    "reveal",
    "output",
    "role",
    "jailbreak",
    "secret",
    "command",
    "developer",
    "policy",
    "hidden",
)

DEFAULT_EXTRACTION_PATTERNS = (
    r"얼마(?:인가요|입니까|야)?",
    r"몇\s*(?:개|건|명|회|번|개월|년|일)",
    r"(?:찾아|추출|알려)\s*(?:줘|주세요)",
    r"what\s+is",
    r"how\s+many",
)

DEFAULT_REGULATORY_PATTERNS = (
    r"규정",
    r"정책",
    r"법령",
    r"기준",
    r"절차",
    r"regulation",
    r"policy",
)

_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{20,}={0,2}(?![A-Za-z0-9+/=])")
_BASE64_RISKS = ("ignore", "system", "prompt", "override", "무시", "출력")


def _matches(pattern: str | re.Pattern[str], text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE)) if isinstance(pattern, str) else bool(pattern.search(text))


def _keyword_present(keyword: str, query: str) -> bool:
    if keyword.isascii() and re.fullmatch(r"[A-Za-z0-9_]+", keyword):
        return bool(re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", query, re.IGNORECASE))
    return keyword.casefold() in query.casefold()


def _suspicious_base64(query: str) -> bool:
    for match in _BASE64.finditer(query):
        token = match.group(0)
        try:
            decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode(
                "utf-8", errors="ignore"
            )
        except (binascii.Error, ValueError, UnicodeError):
            continue
        folded = decoded.casefold()
        if any(word.casefold() in folded for word in _BASE64_RISKS):
            return True
    return False


def classify_question(
    query: str,
    *,
    extraction_patterns: Sequence[str | re.Pattern[str]] = DEFAULT_EXTRACTION_PATTERNS,
    regulatory_patterns: Sequence[str | re.Pattern[str]] = DEFAULT_REGULATORY_PATTERNS,
) -> QuestionClassification:
    """Classify in the required extraction -> regulatory -> explanatory order."""
    extraction_count = sum(_matches(pattern, query) for pattern in extraction_patterns)
    if extraction_count:
        return QuestionClassification("extraction", min(1.0, extraction_count * 0.4), extraction_count)
    regulatory_count = sum(_matches(pattern, query) for pattern in regulatory_patterns)
    if regulatory_count:
        return QuestionClassification("regulatory", min(1.0, regulatory_count * 0.3), regulatory_count)
    return QuestionClassification("explanatory", 0.5, 0)


def detect_injection(
    query: str,
    *,
    patterns: Sequence[str | re.Pattern[str]] = DEFAULT_INJECTION_PATTERNS,
    risk_keywords: Iterable[str] = DEFAULT_RISK_KEYWORDS,
    judge: Callable[[str], object] | None = None,
) -> InjectionResult:
    """Apply pattern, distinct-keyword classifier, then conditional judge."""
    if any(_matches(pattern, query) for pattern in patterns):
        return InjectionResult(True, "pattern_match", 1.0, (), False, False)

    suspicious = _suspicious_base64(query)
    distinct = tuple(dict.fromkeys(word for word in risk_keywords if _keyword_present(word, query)))
    score = min(1.0, len(distinct) / 8.7)
    if score > 0.8:
        return InjectionResult(True, "classifier", score, distinct, False, suspicious)

    call_judge = suspicious or score > 0.5
    if not call_judge or judge is None:
        return InjectionResult(False, None, score, distinct, False, suspicious)

    raw = str(judge(query)).strip()
    upper = raw.upper()
    safe_override = upper.startswith("SAFE") or "판단: SAFE" in upper or "판단:SAFE" in upper
    blocked = "INJECTION" in raw and not safe_override
    return InjectionResult(blocked, "llm_judge" if blocked else None, score, distinct, True, suspicious)
