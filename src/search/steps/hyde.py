"""Generate text used only as the original vector query."""

import random
import time

from src.search.calls import llm_complete
from src.search.core import StepOutcome
from src.shared import SearchUnavailable


def handle(context):
    ports = context.get("ports")
    query = str(context.get("query", ""))
    if ports is None:
        return None
    try:
        context["hyde_query"] = llm_complete(
            ports.llm,
            task="hyde",
            prompt=query,
            settings=context["settings"],
            model_key="hyde_model",
            sleep=context.get("sleep", time.sleep),
            jitter=context.get("jitter", random.uniform),
            breaker=context.get("llm_breaker"),
        )
        return StepOutcome()
    except SearchUnavailable:
        context["hyde_query"] = query
        return StepOutcome(passed=False, detail={"fallback": "original_query"})
