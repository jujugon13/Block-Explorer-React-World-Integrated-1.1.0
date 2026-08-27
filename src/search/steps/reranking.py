"""Rerank a bounded candidate window and degrade atomically on failure."""

import math

from src.search.core import StepOutcome


def handle(context):
    ports = context.get("ports")
    if ports is None or ports.reranker is None:
        return None
    settings = context["settings"]
    results = list(context.get("results", []))
    top_k = int(settings.get("reranker_top_k", 8))
    candidates = results[: top_k * 4]
    try:
        raw_scores = tuple(
            ports.reranker.score(
                str(context.get("query", "")),
                [str(item["content"]) for item in candidates],
                model=str(settings.get("reranker_model", "")),
            )
        )
        if len(raw_scores) != len(candidates):
            raise ValueError("reranker result count mismatch")
        mode = str(settings.get("reranker_score_mode", "calibrated"))
        alpha = float(settings.get("reranker_alpha", 0.7))
        ranked = []
        for rank, (item, raw) in enumerate(zip(candidates, raw_scores)):
            score = float(raw)
            if mode == "calibrated":
                score = alpha * (1.0 / (1.0 + math.exp(-score))) + (1.0 - alpha) / (rank + 1)
            copied = dict(item)
            copied["score"] = score
            ranked.append(copied)
        context["results"] = sorted(
            ranked, key=lambda item: float(item["score"]), reverse=True
        )[:top_k]
        return StepOutcome()
    except Exception:
        context["results"] = results
        return StepOutcome(passed=False, detail={"fallback": "basic_results"})
