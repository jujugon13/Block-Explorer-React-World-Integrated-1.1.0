"""Execute the selected retrieval mode for original and variant queries."""

import random
import time
from concurrent.futures import ThreadPoolExecutor

from src.search.calls import llm_complete
from src.search.core import StepOutcome
from src.search.retrieval import keyword_hits, rrf, sufficient, vector_hits


def handle(context):
    if context.get("ports") is None:
        return None
    queries = list(context.get("query_list") or [context.get("query", "")])
    mode = str(context.get("active_mode", "hybrid"))

    def execute(item):
        index, query = item
        return _one(context, str(query), mode if index == 0 else "hybrid", index == 0)

    with ThreadPoolExecutor(max_workers=max(1, len(queries))) as executor:
        completed = list(executor.map(execute, enumerate(queries)))
    context["search_batches"] = [batch for batch, _ in completed]
    stages = completed[0][1] if mode == "cascading" else None
    return StepOutcome(
        mode_trace_stages=stages,
        variant_query_count=max(0, len(completed) - 1),
    )


def _one(context, query, mode, original):
    settings = context["settings"]
    constant = float(settings.get("rrf_constant", 60))
    vector_query = str(context.get("hyde_query", query)) if original else query
    if mode == "vector":
        return vector_hits(context, vector_query), None
    if mode == "keyword":
        return keyword_hits(context, query), None
    if mode == "cascading":
        first = keyword_hits(context, query)
        if sufficient(first, settings):
            return first, (1,)
        expanded = first
        executed = [1]
        if bool(settings.get("query_expansion_enabled", True)):
            hypothetical = llm_complete(
                context["ports"].llm,
                task="cascading_hypothetical_answer",
                prompt=query,
                settings=settings,
                sleep=context.get("sleep", time.sleep),
                jitter=context.get("jitter", random.uniform),
                breaker=context.get("llm_breaker"),
            )
            keywords = llm_complete(
                context["ports"].llm,
                task="cascading_keyword_extraction",
                prompt=hypothetical,
                settings=settings,
                sleep=context.get("sleep", time.sleep),
                jitter=context.get("jitter", random.uniform),
                breaker=context.get("llm_breaker"),
            )
            limit = int(settings.get("query_expansion_max_keywords", 10))
            expanded_query = " ".join(keywords.split()[:limit]) or query
            expanded = keyword_hits(context, expanded_query)
            executed.append(2)
            if sufficient(expanded, settings):
                return expanded, tuple(executed)
        vector = vector_hits(context, vector_query)
        keyword = expanded or first
        executed.append(3)
        return rrf(
            (
                (vector, float(settings.get("cascading_fallback_vector_weight", 0.3))),
                (keyword, float(settings.get("cascading_fallback_keyword_weight", 0.7))),
            ),
            constant,
        ), tuple(executed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        vector_future = executor.submit(vector_hits, context, vector_query)
        keyword_future = executor.submit(keyword_hits, context, query)
        vector, keyword = vector_future.result(), keyword_future.result()
    return rrf(
        (
            (vector, float(settings.get("vector_weight", 0.5))),
            (keyword, float(settings.get("keyword_weight", 0.5))),
        ),
        constant,
    ), None
