"""Generate bounded variants while preserving the original at index zero."""

import random
import time

from src.search.calls import llm_complete
from src.search.core import StepOutcome
from src.shared import SearchUnavailable


def handle(context):
    ports = context.get("ports")
    query = str(context.get("query", ""))
    if ports is None:
        context.setdefault("query_list", [query])
        return None
    settings = context["settings"]
    try:
        text = llm_complete(
            ports.llm,
            task="multi_query",
            prompt=query,
            settings=settings,
            model_key="multi_query_model",
            sleep=context.get("sleep", time.sleep),
            jitter=context.get("jitter", random.uniform),
            breaker=context.get("llm_breaker"),
        )
        variants = [line.strip(" -\t") for line in text.splitlines() if line.strip(" -\t")]
        bounded = [query]
        for variant in variants:
            if variant not in bounded:
                bounded.append(variant)
            if len(bounded) >= int(settings.get("multi_query_count", 4)):
                break
        context["query_list"] = bounded
        return StepOutcome(detail={"query_count": len(bounded)})
    except SearchUnavailable:
        context["query_list"] = [query]
        return StepOutcome(passed=False, detail={"fallback": "original_query_only"})
