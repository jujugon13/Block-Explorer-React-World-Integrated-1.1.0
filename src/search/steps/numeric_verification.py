"""Record unsupported numeric expressions without mutating the answer."""

from src.search.core import StepOutcome


def handle(context):
    guardrails = getattr(context.get("ports"), "guardrails", None)
    if guardrails is None:
        return None
    evidence = [str(item["content"]) for item in context.get("results", [])]
    result = guardrails.verify_numbers(str(context.get("answer", "")), evidence)
    context["numeric_verification"] = result
    return StepOutcome(
        passed=result.passed, detail={"unsupported": list(result.unsupported)}
    )
