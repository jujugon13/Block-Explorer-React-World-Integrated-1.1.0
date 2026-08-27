"""Mask result content only, leaving the query and final answer untouched."""

from src.search.core import StepOutcome


def handle(context):
    guardrails = getattr(context.get("ports"), "guardrails", None)
    if guardrails is None:
        return None
    result = guardrails.mask_results(context.get("results", []))
    context["results"] = result.results
    return StepOutcome(detail={"detections": result.detections})
