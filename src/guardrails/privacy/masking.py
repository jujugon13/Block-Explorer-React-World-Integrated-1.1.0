"""Priority-ordered PII masking for returned search result bodies."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ..models import PIIMaskingResult


_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _group_mask(match: re.Match[str], keep: int = 1) -> str:
    return "-".join(
        part if index < keep else "*" * len(part)
        for index, part in enumerate(match.groups())
    )


_PATTERNS = (
    ("resident", re.compile(r"\b(\d{6})-([1-4]\d{6})\b"), lambda m: f"{m[1]}-*******"),
    ("foreigner", re.compile(r"\b(\d{6})-([5-8]\d{6})\b"), lambda m: f"{m[1]}-*******"),
    ("driver", re.compile(r"\b(\d{2})-(\d{2})-(\d{6})-(\d{2})\b"), _group_mask),
    (
        "mobile",
        re.compile(r"\b(01[016789])(?:-(\d{3,4})-(\d{4})|(\d{7,8}))\b"),
        lambda m: f"{m[1]}-{'*' * len(m[2])}-****" if m[2] else f"{m[1]}{'*' * len(m[4])}",
    ),
    ("business", re.compile(r"\b(\d{3})-(\d{2})-(\d{5})\b"), _group_mask),
    (
        "telephone",
        re.compile(r"\b(02|0\d{2})-(\d{3,4})-(\d{4})\b"),
        _group_mask,
    ),
    ("passport", re.compile(r"\b([A-Z])(\d{8})\b", re.IGNORECASE), lambda m: f"{m[1]}********"),
    (
        "email",
        re.compile(r"\b([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+)@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE),
        lambda m: f"{m[1][0]}***@{m[2]}",
    ),
    ("account", re.compile(r"\b(\d{2,6})-(\d{2,6})-(\d{2,8})\b"), _group_mask),
)


def mask_pii(text: str) -> tuple[str, int]:
    """Mask non-overlapping matches, preserving the specified pattern priority."""
    date_spans = tuple((m.start(), m.end()) for m in _DATE.finditer(text))
    occupied: list[tuple[int, int]] = []
    replacements: list[tuple[int, int, str]] = []
    for kind, pattern, replacement in _PATTERNS:
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            if kind in {"resident", "foreigner", "account"} and any(
                span[0] < end and start < span[1] for start, end in date_spans
            ):
                continue
            occupied.append(span)
            replacements.append((span[0], span[1], replacement(match)))

    masked = text
    for start, end, replacement in sorted(replacements, reverse=True):
        masked = masked[:start] + replacement + masked[end:]
    return masked, len(replacements)


def mask_results(results: Sequence[Mapping[str, object]]) -> PIIMaskingResult:
    """Copy and mask result content only; query and answer are not accepted here."""
    masked_results: list[dict[str, object]] = []
    detections = 0
    for item in results:
        copy = dict(item)
        content = copy.get("content")
        if isinstance(content, str):
            copy["content"], count = mask_pii(content)
            detections += count
        masked_results.append(copy)
    return PIIMaskingResult(masked_results, detections)
