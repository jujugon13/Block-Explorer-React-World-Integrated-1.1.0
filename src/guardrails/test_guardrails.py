from __future__ import annotations

import unittest

from src.guardrails import (
    GuardrailService,
    check_faithfulness,
    check_hallucination,
    classify_question,
    detect_injection,
    evaluate_retrieval,
    generate_answer,
    mask_pii,
    mask_results,
    verify_numbers,
)


NOT_FOUND = "관련 문서를 충분히 찾지 못했습니다. 다른 키워드로 검색해 주세요."


class GuardrailAcceptanceTests(unittest.TestCase):
    def test_AC_RS_01_seven_distinct_keywords_block(self):
        words = tuple(f"risk{index}" for index in range(7))
        result = detect_injection(" ".join(words), patterns=(), risk_keywords=words)
        self.assertTrue(result.blocked)
        self.assertEqual("classifier", result.reason)
        self.assertGreater(result.score, 0.8)

    def test_AC_RS_02_six_distinct_keywords_pass_classifier(self):
        words = tuple(f"risk{index}" for index in range(6))
        result = detect_injection(
            " ".join(words),
            patterns=(),
            risk_keywords=words,
            judge=lambda query: "SAFE",
        )
        self.assertFalse(result.blocked)
        self.assertEqual(6, len(result.distinct_keywords))
        self.assertLessEqual(result.score, 0.8)

    def test_AC_RS_03_repeated_keyword_counts_once(self):
        calls = []
        result = detect_injection(
            " ".join(["ignore"] * 10),
            patterns=(),
            risk_keywords=("ignore",),
            judge=lambda query: calls.append(query),
        )
        self.assertEqual(("ignore",), result.distinct_keywords)
        self.assertFalse(result.blocked)
        self.assertFalse(result.judge_called)
        self.assertEqual([], calls)

    def test_AC_RS_04_question_classification_is_always_zero_duration(self):
        extraction = classify_question("몇 건입니까")
        explanatory = classify_question("이 문서를 설명해 주세요")
        self.assertEqual(("extraction", 0.4, 0.0), (extraction.kind, extraction.confidence, extraction.duration_ms))
        self.assertEqual(("explanatory", 0.5, 0.0), (explanatory.kind, explanatory.confidence, explanatory.duration_ms))

    def test_AC_RS_09_zero_results_skip_soft_rescue(self):
        calls = []
        gate = evaluate_retrieval([], soft_mode=True, not_found_message=NOT_FOUND)
        answer = generate_answer(
            "질문",
            [],
            question_type="explanatory",
            gate=gate,
            not_found_message=NOT_FOUND,
            evidence_extractor=lambda query, contexts: calls.append(contexts) or "근거",
        )
        self.assertFalse(gate.soft_failed)
        self.assertEqual(NOT_FOUND, answer.answer)
        self.assertFalse(answer.evidence_attempted)
        self.assertEqual([], calls)

    def test_AC_RS_10_resident_number_is_masked_in_result_content(self):
        masked = mask_results(({"content": "주민번호 880101-1234567"},))
        self.assertEqual("주민번호 880101-*******", masked.results[0]["content"])
        self.assertEqual(1, masked.detections)

    def test_AC_RS_11_date_is_not_misclassified_as_account(self):
        text = "시행일은 2024-01-15입니다."
        self.assertEqual((text, 0), mask_pii(text))

    def test_AC_RS_12_generation_receives_masked_content(self):
        seen = []
        masked = mask_results(({"content": "담당자 010-1234-5678", "score": 1.0},))
        service = GuardrailService(
            general_generator=lambda query, contexts: seen.extend(contexts) or "답변"
        )
        result = service.generate_answer(
            "담당자는?",
            masked.results,
            question_type="explanatory",
            gate=evaluate_retrieval(masked.results),
        )
        self.assertEqual("답변", result.answer)
        self.assertEqual(["담당자 010-****-****"], seen)

    def test_AC_RS_13_query_and_final_answer_are_not_masked(self):
        query = "내 번호 010-1234-5678을 그대로 답해"
        service = GuardrailService(general_generator=lambda given, contexts: given)
        result = service.generate_answer(
            query,
            [{"content": "개인정보 없음", "score": 1.0}],
            question_type="explanatory",
            gate=evaluate_retrieval([{"content": "개인정보 없음", "score": 1.0}]),
        )
        self.assertEqual(query, result.answer)

    def test_AC_RS_14_extraction_uses_short_path_then_general_fallback(self):
        gate = evaluate_retrieval([{"content": "근거", "score": 1.0}])
        direct = generate_answer(
            "값은?",
            [{"content": "근거", "score": 1.0}],
            question_type="extraction",
            gate=gate,
            short_answer_extractor=lambda query, contexts: "단답",
            general_generator=lambda query, contexts: self.fail("general path must not run"),
        )
        fallback = generate_answer(
            "값은?",
            [{"content": "근거", "score": 1.0}],
            question_type="extraction",
            gate=gate,
            short_answer_extractor=lambda query, contexts: "",
            general_generator=lambda query, contexts: "일반 답변",
        )
        self.assertEqual(("단답", "short_answer_extraction"), (direct.answer, direct.branch))
        self.assertEqual(("일반 답변", "general_generation"), (fallback.answer, fallback.fallback))
        with self.assertRaisesRegex(RuntimeError, "provider down"):
            generate_answer(
                "값은?",
                [{"content": "근거", "score": 1.0}],
                question_type="extraction",
                gate=gate,
                short_answer_extractor=lambda query, contexts: "",
                general_generator=lambda query, contexts: (_ for _ in ()).throw(RuntimeError("provider down")),
            )

    def test_AC_RS_15_soft_fail_without_evidence_returns_not_found(self):
        calls = []
        results = [{"document_id": "d1", "content": "낮은 근거", "score": 0.1}]
        gate = evaluate_retrieval(results, soft_mode=True, not_found_message=NOT_FOUND)
        result = generate_answer(
            "질문",
            results,
            question_type="explanatory",
            gate=gate,
            not_found_message=NOT_FOUND,
            evidence_extractor=lambda query, contexts: calls.append(contexts) or "",
        )
        self.assertTrue(gate.soft_failed)
        self.assertEqual(NOT_FOUND, result.answer)
        self.assertEqual(1, len(calls))

    def test_AC_RS_16_unsupported_number_does_not_change_answer(self):
        answer = "정원은 10명입니다."
        result = verify_numbers(answer, ("정원은 9명입니다.",))
        self.assertFalse(result.passed)
        self.assertEqual(("10명",), result.unsupported)
        self.assertEqual("정원은 10명입니다.", answer)
        self.assertTrue(verify_numbers("숫자 표현 없음", ()).passed)
        self.assertTrue(verify_numbers("매월 3회 시행", ("매월 3회 시행",)).passed)
        self.assertTrue(verify_numbers("기간은 6개월", ("기간은 반기",)).passed)
        self.assertEqual((), verify_numbers("제3회 행사", ()).expressions)

    def test_AC_RS_17_low_faithfulness_warn_appends_fixed_text(self):
        result = check_faithfulness(
            "원 답변",
            ("근거",),
            judge=lambda answer, evidence: {"score": 0.4, "distortions": ["기간"]},
            threshold=0.9,
            action="warn",
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.answer.startswith("원 답변\n"))
        self.assertIn("⚠️ 이 답변에 원문과 다른 표현이 포함되어 있을 수 있습니다. (충실도: 40%)", result.answer)
        self.assertIn("주의 항목: 기간", result.answer)

        service = GuardrailService(faithfulness_judge=lambda answer, evidence: 0.85)
        passed = service.judge_faithfulness(
            "원 답변",
            ("근거",),
            None,
            {"faithfulness_threshold": 0.8, "faithfulness_action": "warn"},
        )
        blocked = GuardrailService(
            faithfulness_judge=lambda answer, evidence: 0.4
        ).judge_faithfulness(
            "원 답변",
            ("근거",),
            None,
            {"faithfulness_threshold": 0.9, "faithfulness_action": "block"},
        )
        self.assertTrue(passed.passed)
        self.assertEqual("원 답변", passed.answer)
        self.assertEqual(
            "답변의 정확성을 보장할 수 없습니다. 원문을 직접 확인해 주세요.",
            blocked.answer,
        )

    def test_AC_RS_18_hallucination_block_setting_still_appends_warning(self):
        result = check_hallucination(
            "원 답변",
            ("근거",),
            judge=lambda answer, evidence: 0.2,
            threshold=0.8,
            action="block",
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.answer.startswith("원 답변\n⚠️"))
        self.assertIn("근거 비율: 20%", result.answer)

        class LLM:
            def __init__(self):
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                return "0.3"

        llm = LLM()
        flat = GuardrailService().judge_hallucination(
            "원 답변",
            ("근거",),
            llm,
            {
                "hallucination_threshold": 0.2,
                "hallucination_judge_model": "flat-judge",
            },
        )
        self.assertTrue(flat.passed)
        self.assertEqual("원 답변", flat.answer)
        self.assertEqual("flat-judge", llm.requests[0].model)

    def test_AC_RS_19_unparseable_judges_fail_open(self):
        faithfulness = check_faithfulness(
            "답변", (), judge=lambda answer, evidence: "분석할 수 없음"
        )
        hallucination = check_hallucination(
            "답변", (), judge=lambda answer, evidence: {"unexpected": "value"}
        )
        for result in (faithfulness, hallucination):
            self.assertTrue(result.passed)
            self.assertTrue(result.parse_failed)
            self.assertEqual(1.0, result.score)
            self.assertEqual("답변", result.answer)

    def test_AC_RS_19_llm_judge_uses_four_attempt_retry_policy(self):
        class APIError(RuntimeError):
            def __init__(self, status_code=None):
                super().__init__(status_code)
                self.status_code = status_code

        class LLM:
            def __init__(self, outcomes):
                self.outcomes = iter(outcomes)
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                outcome = next(self.outcomes)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        waits = []
        llm = LLM([APIError(429), APIError(503), APIError(), "0.4"])
        service = GuardrailService(sleep=waits.append, jitter=lambda low, high: low)
        result = service.judge_faithfulness(
            "답변", ("근거",), llm, {"faithfulness": {"threshold": 0.9, "action": "warn"}}
        )
        self.assertEqual(4, len(llm.requests))
        self.assertEqual([0.5, 1.0, 2.0], waits)
        self.assertEqual(0.4, result.score)

        exhausted = LLM([APIError()] * 4)
        waits.clear()
        result = service.judge_hallucination(
            "답변", ("근거",), exhausted, {"hallucination": {"threshold": 0.8}}
        )
        self.assertEqual(4, len(exhausted.requests))
        self.assertEqual([0.5, 1.0, 2.0], waits)
        self.assertTrue(result.passed)
        self.assertTrue(result.parse_failed)

        non_retryable = LLM([APIError(400), "0.1"])
        waits.clear()
        result = service.judge_hallucination(
            "답변", ("근거",), non_retryable, {"hallucination": {"threshold": 0.8}}
        )
        self.assertEqual(1, len(non_retryable.requests))
        self.assertEqual([], waits)
        self.assertTrue(result.passed)

        waits.clear()
        injection_llm = LLM([APIError(529), "SAFE"])
        injection_service = GuardrailService(
            injection_patterns=(),
            risk_keywords=("risk0", "risk1", "risk2", "risk3", "risk4"),
            sleep=waits.append,
            jitter=lambda low, high: low,
        )
        result = injection_service.inspect_input(
            "risk0 risk1 risk2 risk3 risk4", injection_llm, {}
        )
        self.assertEqual(2, len(injection_llm.requests))
        self.assertEqual([0.5], waits)
        self.assertTrue(result.judge_called)
        self.assertFalse(result.blocked)

    def test_AC_RS_19_guardrail_retries_share_one_logical_breaker_permit(self):
        class Breaker:
            def __init__(self):
                self.before = self.success = self.failure = 0

            def before_call(self):
                self.before += 1
                return object()

            def record_success(self, permit):
                self.success += 1

            def record_failure(self, permit):
                self.failure += 1

        outcomes = iter((RuntimeError("1"), RuntimeError("2"), "0.95"))

        class LLM:
            def complete(self, request):
                outcome = next(outcomes)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        breaker = Breaker()
        result = GuardrailService(
            sleep=lambda seconds: None
        ).judge_faithfulness(
            "답변", ("근거",), LLM(), {}, breaker
        )
        self.assertTrue(result.passed)
        self.assertEqual((1, 1, 0), (breaker.before, breaker.success, breaker.failure))

        class FailingLLM:
            def complete(self, request):
                raise RuntimeError("down")

        breaker = Breaker()
        result = GuardrailService(
            sleep=lambda seconds: None
        ).judge_hallucination(
            "답변", ("근거",), FailingLLM(), {}, breaker
        )
        self.assertTrue(result.passed)
        self.assertEqual((1, 0, 1), (breaker.before, breaker.success, breaker.failure))


if __name__ == "__main__":
    unittest.main()
