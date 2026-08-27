"""Classify a question without using confidence for branching."""

import re


def handle(context):
    query = str(context.get("query", ""))
    guardrails = getattr(context.get("ports"), "guardrails", None)
    if guardrails is not None and hasattr(guardrails, "classify_question"):
        result = guardrails.classify_question(query)
        context["question_type"] = result.kind
        context["question_confidence"] = result.confidence
        context["question_indicator_count"] = result.indicator_count
        return None
    extraction = len(re.findall(r"(?:얼마|몇|무엇|누구|언제|어디|찾아|알려)", query))
    regulatory = len(re.findall(r"(?:규정|법령|법률|정책|기준|의무|금지)", query))
    if extraction:
        kind, confidence, count = "extraction", min(1.0, extraction * 0.4), extraction
    elif regulatory:
        kind, confidence, count = "regulatory", min(1.0, regulatory * 0.3), regulatory
    else:
        kind, confidence, count = "explanatory", 0.5, 0
    context["question_type"] = kind
    context["question_confidence"] = confidence
    context["question_indicator_count"] = count
    return None
