"""Run the three-layer input inspection and terminate on a block."""

from src.search.core import StepOutcome


def handle(context):
    ports = context.get("ports")
    guardrails = getattr(ports, "guardrails", None)
    if guardrails is None:
        return None
    result = guardrails.inspect_input(
        str(context.get("query", "")),
        ports.llm,
        context["settings"],
        context.get("llm_breaker"),
    )
    detail = {
        "reason": result.reason,
        "score": result.score,
        "judge_called": result.judge_called,
    }
    if result.blocked:
        context["public_error"] = ("GUARDRAIL_VIOLATION", result.reason or "classifier")
        return StepOutcome(passed=False, detail=detail, terminate=True)
    return StepOutcome(passed=not result.blocked, detail=detail)
