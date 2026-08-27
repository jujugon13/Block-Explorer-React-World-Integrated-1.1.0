"""Pure retrieval, generation, numeric, and answer guardrail operations."""

from __future__ import annotations

import json
import math
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from numbers import Real

from src.shared import LanguageModelRequest

from ..input import (
    DEFAULT_EXTRACTION_PATTERNS,
    DEFAULT_INJECTION_PATTERNS,
    DEFAULT_REGULATORY_PATTERNS,
    DEFAULT_RISK_KEYWORDS,
    classify_question,
    detect_injection,
)
from ..models import (
    AnswerGuardrailResult,
    GenerationResult,
    NumericVerification,
    RetrievalGateResult,
)
from ..privacy import mask_results


_NUMBER = re.compile(
    r"(?<!\w)(?:(?:매(?:연|월|주|일|반기|분기)?|연|월|주|일|반기|분기)\s*)?"
    r"\d+(?:,\d{3})*(?:\.\d+)?\s*"
    r"(?:개월|반기|분기|시간|회|번|개|점|%|원|일|주|월|년|건|명|만|억|천)"
)
_FAITHFULNESS_BLOCK = "답변의 정확성을 보장할 수 없습니다. 원문을 직접 확인해 주세요."
_RETRYABLE_LLM_STATUS = frozenset({429, 500, 502, 503, 529})


def evaluate_retrieval(
    results: Sequence[Mapping[str, object]],
    *,
    min_top_score: float = 0.3,
    min_doc_count: int = 1,
    min_doc_score: float = 0.2,
    soft_mode: bool = True,
    not_found_message: str = "관련 문서를 충분히 찾지 못했습니다. 다른 키워드로 검색해 주세요.",
) -> RetrievalGateResult:
    """Apply the zero-result exception before the configurable soft path."""
    if not results:
        return RetrievalGateResult(False, False, not_found_message, "no_results")
    scores = [float(item.get("score", 0.0)) for item in results]
    qualified_documents = {
        item.get("document_id", item.get("documentId", index))
        for index, item in enumerate(results)
        if float(item.get("score", 0.0)) >= min_doc_score
    }
    reason = None
    if max(scores) < min_top_score:
        reason = "top_score"
    elif len(qualified_documents) < min_doc_count:
        reason = "document_count"
    if reason is None:
        return RetrievalGateResult(True, False, None, "passed")
    return RetrievalGateResult(
        False,
        soft_mode,
        None if soft_mode else not_found_message,
        reason,
    )


def _contexts(results: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    return tuple(str(item["content"]) for item in results if isinstance(item.get("content"), str))


def _try_extract(
    port: Callable[[str, tuple[str, ...]], object] | None,
    query: str,
    contexts: tuple[str, ...],
) -> tuple[str, bool]:
    if port is None:
        return "", True
    try:
        value = port(query, contexts)
    except Exception:
        return "", True
    if not isinstance(value, str):
        return "", True
    return value.strip(), False


def _generate(
    port: Callable[[str, tuple[str, ...]], object] | None,
    query: str,
    contexts: tuple[str, ...],
) -> str:
    if port is None:
        raise RuntimeError("general generation port is required")
    value = port(query, contexts)
    if not isinstance(value, str):
        raise TypeError("general generation must return a string")
    return value


def generate_answer(
    query: str,
    results: Sequence[Mapping[str, object]],
    *,
    question_type: str,
    gate: RetrievalGateResult,
    exact_citation_enabled: bool = True,
    not_found_message: str = "관련 문서를 충분히 찾지 못했습니다. 다른 키워드로 검색해 주세요.",
    evidence_extractor: Callable[[str, tuple[str, ...]], object] | None = None,
    short_answer_extractor: Callable[[str, tuple[str, ...]], object] | None = None,
    general_generator: Callable[[str, tuple[str, ...]], object] | None = None,
) -> GenerationResult:
    """Execute the four generation branches in their specified priority."""
    contexts = _contexts(results)
    if gate.terminal_answer is not None:
        return GenerationResult(gate.terminal_answer, "gate_terminal")
    if gate.soft_failed:
        extracted, failed = _try_extract(evidence_extractor, query, contexts)
        if extracted:
            return GenerationResult(extracted, "evidence_extraction", evidence_attempted=True, rescued=True)
        if failed:
            generated = _generate(general_generator, query, contexts)
            return GenerationResult(generated, "general_generation", "general_generation", True)
        return GenerationResult(not_found_message, "evidence_extraction", evidence_attempted=True)
    if exact_citation_enabled and question_type == "regulatory":
        extracted, _ = _try_extract(evidence_extractor, query, contexts)
        if extracted:
            return GenerationResult(extracted, "evidence_extraction", evidence_attempted=True)
        generated = _generate(general_generator, query, contexts)
        return GenerationResult(generated, "general_generation", "general_generation", True)
    if question_type == "extraction":
        extracted, _ = _try_extract(short_answer_extractor, query, contexts)
        if extracted:
            return GenerationResult(extracted, "short_answer_extraction")
        generated = _generate(general_generator, query, contexts)
        return GenerationResult(generated, "general_generation", "general_generation")
    return GenerationResult(_generate(general_generator, query, contexts), "general_generation")


def _normalized(text: str) -> str:
    return re.sub(r"[,\s]+", "", text)


def _equivalents(expression: str) -> tuple[str, ...]:
    normalized = _normalized(expression)
    equivalents = {
        "1반기": ("6개월", "반년"),
        "6개월": ("1반기", "반기", "반년"),
        "1분기": ("3개월", "분기"),
        "3개월": ("1분기", "분기"),
        "1년": ("연", "12개월"),
        "12개월": ("연", "1년"),
        "30일": ("월",),
    }
    alternatives = list(equivalents.get(normalized, ()))
    candidates = [normalized]
    if normalized.startswith("매") and len(normalized) > 1:
        candidates.append(normalized[1:])
        alternatives.append(normalized[1:])
    for candidate in candidates:
        for prefix, replacements in {
            "반기": ("6개월",),
            "분기": ("3개월",),
            "연": ("1년", "12개월"),
            "월": ("30일",),
        }.items():
            if candidate.startswith(prefix):
                alternatives.extend(replacement + candidate[len(prefix):] for replacement in replacements)
                break
    return tuple(dict.fromkeys(alternatives))


def verify_numbers(answer: str, evidence_texts: Sequence[str]) -> NumericVerification:
    """Report unsupported number/unit expressions without changing the answer."""
    expressions = tuple(match.group(0).strip() for match in _NUMBER.finditer(answer))
    normalized_evidence = tuple(_normalized(text) for text in evidence_texts)
    unsupported: list[str] = []
    for expression in expressions:
        normalized = _normalized(expression)
        supported = any(normalized in evidence for evidence in normalized_evidence)
        if not supported:
            supported = any(expression in evidence for evidence in evidence_texts)
        if not supported:
            supported = any(
                equivalent in evidence
                for equivalent in _equivalents(expression)
                for evidence in normalized_evidence
            )
        if not supported:
            unsupported.append(expression)
    return NumericVerification(not unsupported, expressions, tuple(unsupported))


def _parsed_score(raw: object) -> tuple[float, bool, tuple[str, ...]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            try:
                raw = float(raw.strip())
            except ValueError:
                return 1.0, True, ()
    distortions: tuple[str, ...] = ()
    if isinstance(raw, Mapping):
        values = raw
        raw = next(
            (values[key] for key in ("score", "faithfulness_score", "hallucination_score", "groundedness") if key in values),
            None,
        )
        listed = values.get("distortions", ())
        if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes)):
            distortions = tuple(str(item) for item in listed)
    if isinstance(raw, bool) or not isinstance(raw, Real) or not math.isfinite(raw) or not 0.0 <= float(raw) <= 1.0:
        return 1.0, True, ()
    return float(raw), False, distortions


def _judge(
    judge: Callable[[str, Sequence[str]], object] | None,
    answer: str,
    evidence_texts: Sequence[str],
) -> tuple[float, bool, tuple[str, ...]]:
    if judge is None:
        return 1.0, True, ()
    try:
        return _parsed_score(judge(answer, evidence_texts))
    except Exception:
        return 1.0, True, ()


def check_faithfulness(
    answer: str,
    evidence_texts: Sequence[str],
    *,
    judge: Callable[[str, Sequence[str]], object] | None,
    threshold: float = 0.9,
    action: str = "warn",
) -> AnswerGuardrailResult:
    score, parse_failed, distortions = _judge(judge, answer, evidence_texts)
    passed = score >= threshold
    if passed:
        return AnswerGuardrailResult(answer, True, score, parse_failed)
    if action == "block":
        changed = _FAITHFULNESS_BLOCK
    elif action == "warn":
        changed = answer + f"\n⚠️ 이 답변에 원문과 다른 표현이 포함되어 있을 수 있습니다. (충실도: {round(score * 100)}%)"
        if distortions:
            changed += "\n주의 항목: " + ", ".join(distortions)
    else:
        raise ValueError("faithfulness action must be warn or block")
    return AnswerGuardrailResult(changed, False, score, parse_failed)


def check_hallucination(
    answer: str,
    evidence_texts: Sequence[str],
    *,
    judge: Callable[[str, Sequence[str]], object] | None,
    threshold: float = 0.8,
    action: str = "warn",
) -> AnswerGuardrailResult:
    del action  # The specified action is deliberately ignored on this path.
    score, parse_failed, _ = _judge(judge, answer, evidence_texts)
    passed = score >= threshold
    if passed:
        return AnswerGuardrailResult(answer, True, score, parse_failed)
    changed = answer + f"\n⚠️ 이 답변의 일부는 제공된 문서에서 확인되지 않았습니다. (근거 비율: {round(score * 100)}%)"
    return AnswerGuardrailResult(changed, False, score, parse_failed)


class GuardrailService:
    """Context-injected facade; ports are plain callables, never concrete clients."""

    def __init__(
        self,
        *,
        injection_patterns=DEFAULT_INJECTION_PATTERNS,
        risk_keywords=DEFAULT_RISK_KEYWORDS,
        extraction_patterns=DEFAULT_EXTRACTION_PATTERNS,
        regulatory_patterns=DEFAULT_REGULATORY_PATTERNS,
        injection_judge=None,
        evidence_extractor=None,
        short_answer_extractor=None,
        general_generator=None,
        faithfulness_judge=None,
        hallucination_judge=None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.injection_patterns = tuple(injection_patterns)
        self.risk_keywords = tuple(risk_keywords)
        self.extraction_patterns = tuple(extraction_patterns)
        self.regulatory_patterns = tuple(regulatory_patterns)
        self.injection_judge = injection_judge
        self.evidence_extractor = evidence_extractor
        self.short_answer_extractor = short_answer_extractor
        self.general_generator = general_generator
        self.faithfulness_judge = faithfulness_judge
        self.hallucination_judge = hallucination_judge
        self._sleep = sleep
        self._jitter = jitter

    @staticmethod
    def _setting(settings: Mapping[str, object], path: str, default: object) -> object:
        if path in settings:
            return settings[path]
        alias = path.replace(".", "_")
        if alias in settings:
            return settings[alias]
        current: object = settings
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current

    def _retry_judge(self, call: Callable[[], object], breaker=None) -> object:
        permit = breaker.before_call() if breaker is not None else None
        try:
            for attempt in range(4):
                try:
                    result = call()
                    if breaker is not None:
                        breaker.record_success(permit)
                    return result
                except Exception as error:
                    status = getattr(error, "status_code", getattr(error, "status", None))
                    if status is not None and status not in _RETRYABLE_LLM_STATUS:
                        raise
                    if attempt == 3:
                        raise
                    self._sleep(min(1.0 * 2**attempt, 30.0) * self._jitter(0.5, 1.0))
        except Exception:
            if breaker is not None:
                breaker.record_failure(permit)
            raise
        raise AssertionError("unreachable")

    def _retrying(self, judge, breaker=None):
        return None if judge is None else lambda *args: self._retry_judge(lambda: judge(*args), breaker)

    def _llm_request(self, llm, task: str, prompt: str, settings: Mapping[str, object], breaker=None):
        if llm is None:
            return None

        def judge(*_values):
            model = str(self._setting(settings, f"{task}.judge_model", settings.get("llm_model", "gpt-4.1-mini")))

            def complete():
                response = llm.complete(
                    LanguageModelRequest(task, prompt, model, 0.0, 25.0)
                )
                if not isinstance(response, str):
                    raise TypeError("LLM response must be text")
                return response

            return self._retry_judge(complete, breaker)

        return judge

    def inspect_input(self, query: str, llm=None, settings: Mapping[str, object] | None = None, breaker=None):
        config = settings or {}
        judge = self._retrying(self.injection_judge, breaker) or self._llm_request(llm, "injection", query, config, breaker)
        return detect_injection(
            query,
            patterns=self.injection_patterns,
            risk_keywords=self.risk_keywords,
            judge=judge,
        )

    def classify_question(self, query: str):
        return classify_question(
            query,
            extraction_patterns=self.extraction_patterns,
            regulatory_patterns=self.regulatory_patterns,
        )

    evaluate_retrieval = staticmethod(evaluate_retrieval)
    mask_results = staticmethod(mask_results)
    verify_numbers = staticmethod(verify_numbers)

    def generate_answer(self, query, results, **options):
        return generate_answer(
            query,
            results,
            evidence_extractor=self.evidence_extractor,
            short_answer_extractor=self.short_answer_extractor,
            general_generator=self.general_generator,
            **options,
        )

    def check_faithfulness(self, answer, evidence_texts, **options):
        return check_faithfulness(
            answer, evidence_texts, judge=self._retrying(self.faithfulness_judge), **options
        )

    def check_hallucination(self, answer, evidence_texts, **options):
        return check_hallucination(
            answer, evidence_texts, judge=self._retrying(self.hallucination_judge), **options
        )

    def judge_faithfulness(self, answer, evidence_texts, llm, settings, breaker=None):
        config = settings or {}
        prompt = json.dumps(
            {"answer": answer, "evidence": list(evidence_texts)}, ensure_ascii=False
        )
        judge = self._retrying(self.faithfulness_judge, breaker) or self._llm_request(llm, "faithfulness", prompt, config, breaker)
        return check_faithfulness(
            answer,
            evidence_texts,
            judge=judge,
            threshold=float(self._setting(config, "faithfulness.threshold", 0.9)),
            action=str(self._setting(config, "faithfulness.action", "warn")),
        )

    def judge_hallucination(self, answer, evidence_texts, llm, settings, breaker=None):
        config = settings or {}
        prompt = json.dumps(
            {"answer": answer, "evidence": list(evidence_texts)}, ensure_ascii=False
        )
        judge = self._retrying(self.hallucination_judge, breaker) or self._llm_request(llm, "hallucination", prompt, config, breaker)
        return check_hallucination(
            answer,
            evidence_texts,
            judge=judge,
            threshold=float(self._setting(config, "hallucination.threshold", 0.8)),
            action=str(self._setting(config, "hallucination.action", "warn")),
        )
