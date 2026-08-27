"""Apply the configured faithfulness action to an existing answer."""

from src.search.core import StepOutcome


def handle(context):
    ports = context.get("ports")
    guardrails = getattr(ports, "guardrails", None)
    if guardrails is None:
        return None
    evidence = [str(item["content"]) for item in context.get("results", [])]
    result = guardrails.judge_faithfulness(
        str(context.get("answer", "")),
        evidence,
        ports.llm,
        context["settings"],
        context.get("llm_breaker"),
    )
    context["answer"] = result.answer
    return StepOutcome(
        passed=result.passed,
        detail={"score": result.score, "parse_failed": result.parse_failed},
    )
