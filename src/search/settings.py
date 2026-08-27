"""Search defaults and request-local override normalization from S11/S14."""

from __future__ import annotations

from collections.abc import Mapping


DEFAULTS: dict[str, object] = {
    "search_mode": "hybrid",
    "keyword_engine": "elasticsearch",
    "rrf_constant": 60,
    "vector_weight": 0.5,
    "keyword_weight": 0.5,
    "reranking_enabled": True,
    "reranker_model": "dragonkue/bge-reranker-v2-m3-ko",
    "reranker_top_k": 8,
    "retriever_top_k": 20,
    "reranker_score_mode": "calibrated",
    "reranker_alpha": 0.7,
    "hyde_enabled": True,
    "hyde_model": "gpt-4.1-mini",
    "cascading_bm25_threshold": 3.0,
    "cascading_min_qualifying_docs": 3,
    "cascading_min_doc_score": 1.0,
    "cascading_fallback_vector_weight": 0.3,
    "cascading_fallback_keyword_weight": 0.7,
    "query_expansion_enabled": True,
    "query_expansion_max_keywords": 10,
    "document_scope_enabled": True,
    "document_scope_top_n": 3,
    "multi_query_enabled": True,
    "multi_query_count": 4,
    "multi_query_model": "gpt-4.1-mini",
    "exact_citation_enabled": True,
    "numeric_verification_enabled": True,
    "cache_enabled": True,
    "cache_search_ttl": 3600,
    "embedding_model": "text-embedding-3-small",
    "llm_provider": "openai",
    "llm_model": "gpt-4.1-mini",
    "llm_temperature": 0.3,
    "system_prompt": "",
    "pii_detection_enabled": True,
    "injection_detection_enabled": True,
    "hallucination_detection_enabled": True,
    "retrieval_quality_gate_enabled": True,
    "faithfulness_enabled": True,
    "generate_answer": True,
    "injection_action": "block",
    "injection_block_message": "이 질문은 처리할 수 없습니다.",
    "hallucination_threshold": 0.8,
    "hallucination_judge_model": "gpt-4.1-mini",
    "min_top_score": 0.3,
    "min_doc_count": 1,
    "min_doc_score": 0.2,
    "soft_mode": True,
    "not_found_message": "관련 문서를 충분히 찾지 못했습니다. 다른 키워드로 검색해 주세요.",
    "faithfulness_threshold": 0.9,
    "faithfulness_action": "warn",
}


_NESTED_FLAGS = {
    "injection_detection_enabled": "injection",
    "pii_detection_enabled": "pii",
    "hallucination_detection_enabled": "hallucination",
    "retrieval_quality_gate_enabled": "retrieval_quality_gate",
    "faithfulness_enabled": "faithfulness",
}


def effective_settings(stored: Mapping[str, object] | None = None) -> dict[str, object]:
    settings = dict(DEFAULTS)
    supplied = dict(stored or {})
    settings.update(supplied)
    for flat, nested_name in _NESTED_FLAGS.items():
        nested = supplied.get(nested_name)
        if flat in supplied:
            if isinstance(nested, Mapping):
                copied = dict(nested)
                copied["enabled"] = bool(supplied[flat])
                settings[nested_name] = copied
        elif isinstance(nested, Mapping) and "enabled" in nested:
            settings[flat] = bool(nested["enabled"])
    return settings

