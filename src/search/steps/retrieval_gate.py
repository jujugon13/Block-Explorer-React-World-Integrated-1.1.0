"""Decide whether retrieval passes, soft-fails, or terminates."""

from src.search.core import StepOutcome


def handle(context):
    if context.get("ports") is None:
        return None
    settings = context["settings"]
    results = list(context.get("results", []))
    guardrails = getattr(context.get("ports"), "guardrails", None)
    if guardrails is not None and hasattr(guardrails, "evaluate_retrieval"):
        verdict = guardrails.evaluate_retrieval(
            results,
            min_top_score=float(settings.get("min_top_score", 0.3)),
            min_doc_count=int(settings.get("min_doc_count", 1)),
            min_doc_score=float(settings.get("min_doc_score", 0.2)),
            soft_mode=bool(settings.get("soft_mode", True)),
            not_found_message=str(settings.get("not_found_message", "")),
        )
        if verdict.passed:
            context["gate"] = "pass"
            return StepOutcome()
        if verdict.soft_failed:
            context["gate"] = "soft_fail"
            return StepOutcome(passed=False, detail={"reason": verdict.reason})
        context["gate"] = "hard_fail"
        context["answer"] = verdict.terminal_answer or ""
        return StepOutcome(
            passed=False, detail={"reason": verdict.reason}, terminate=True
        )
    if not results:
        return _terminal(context, settings, "no_results")
    top_score = max(float(item["score"]) for item in results)
    by_document = {}
    for item in results:
        key = item["document_id"]
        by_document[key] = max(by_document.get(key, float("-inf")), float(item["score"]))
    enough_documents = sum(
        score >= float(settings.get("min_doc_score", 0.2))
        for score in by_document.values()
    ) >= int(settings.get("min_doc_count", 1))
    if top_score >= float(settings.get("min_top_score", 0.3)) and enough_documents:
        context["gate"] = "pass"
        return StepOutcome()
    if bool(settings.get("soft_mode", True)):
        context["gate"] = "soft_fail"
        return StepOutcome(passed=False, detail={"reason": "quality"})
    return _terminal(context, settings, "quality")


def _terminal(context, settings, reason):
    context["gate"] = "hard_fail"
    context["answer"] = str(settings.get("not_found_message", ""))
    return StepOutcome(passed=False, detail={"reason": reason}, terminate=True)
