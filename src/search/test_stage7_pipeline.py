from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.search import DEFAULT_DEFINITION, PipelineInterpreter
from tools.verify_pipeline import verify_trace


ALL_ENABLED = {
    "injection_detection_enabled": True,
    "multi_query_enabled": True,
    "hyde_enabled": True,
    "document_scope_enabled": True,
    "document_scope_top_n": 1,
    "reranking_enabled": True,
    "retrieval_quality_gate_enabled": True,
    "pii_detection_enabled": True,
    "generate_answer": True,
    "numeric_verification_enabled": True,
    "faithfulness_enabled": True,
    "hallucination_detection_enabled": True,
    "search_mode": "hybrid",
}


class StageSevenPipelineTests(unittest.TestCase):
    def test_AC_RS_28_definition_trace_sequence(self):
        state = {
            "results": [
                {"document_id": "document-a", "score": 0.9},
                {"document_id": "document-b", "score": 0.8},
            ],
            "answer": "placeholder",
        }
        run = PipelineInterpreter(timer=lambda: 0.0).run(state, settings=ALL_ENABLED)
        names = [item["name"] for item in run["pipeline_trace"]]
        self.assertEqual(
            [
                "guardrail_input",
                "question_classification",
                "multi_query",
                "hyde",
                "permission_prefilter",
                "vector_search",
                "keyword_search",
                "rrf_fusion",
                "permission_livecheck",
                "document_scope",
                "reranking",
                "retrieval_gate",
                "guardrail_pii",
                "generation",
                "numeric_verification",
                "guardrail_faithfulness",
                "guardrail_hallucination",
            ],
            names,
        )
        classification = run["pipeline_trace"][1]
        self.assertEqual(0.0, classification["duration_ms"])
        self.assertEqual(2, classification["results_count"])

    def test_AC_RS_28_json_ids_match_sixteen_step_modules(self):
        interpreter = PipelineInterpreter(timer=lambda: 0.0)
        expected = {str(step["id"]) for step in interpreter.steps}
        directory = Path(__file__).with_name("steps")
        actual = {
            path.stem
            for path in directory.glob("*.py")
            if path.name != "__init__.py"
        }
        self.assertEqual(16, len(expected))
        self.assertEqual(expected, actual)

    def test_AC_RS_32_disabled_flags_keep_mandatory_definition_steps(self):
        settings = {
            key: False
            for key in ALL_ENABLED
            if key.endswith("_enabled") or key == "generate_answer"
        }
        settings["search_mode"] = "vector"
        run = PipelineInterpreter(timer=lambda: 0.0).run({}, settings=settings)
        self.assertEqual(
            [
                "question_classification",
                "permission_prefilter",
                "vector_search",
                "permission_livecheck",
            ],
            [item["name"] for item in run["pipeline_trace"]],
        )

    def test_AC_RS_28_verifier_accepts_subset_and_rejects_reordered_trace(self):
        run = PipelineInterpreter(timer=lambda: 0.0).run(
            {"results": [], "answer": ""}, settings={"search_mode": "keyword"}
        )
        definition = json.loads(DEFAULT_DEFINITION.read_text(encoding="utf-8"))
        trace = run["pipeline_trace"]
        self.assertEqual([], verify_trace(trace, definition))
        self.assertIn("순서", " ".join(verify_trace(list(reversed(trace)), definition)))


if __name__ == "__main__":
    unittest.main()
