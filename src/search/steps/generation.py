"""Generate an answer through the priority branches within one deadline."""

import json
import random
import time

from src.search.calls import llm_complete
from src.search.core import StepOutcome
from src.shared import GenerationDeadlineExceeded, SearchUnavailable


def handle(context):
    ports = context.get("ports")
    if ports is None:
        return None
    settings = context["settings"]
    clock = context.get("clock", time.monotonic)
    definition = context["pipeline_definition"]
    delivery = definition.get("delivery_mode", {})
    deadline_seconds = delivery.get("generation_deadline_seconds")
    if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)):
        raise ValueError("generation deadline is missing from the pipeline definition")
    deadline_seconds = float(deadline_seconds)
    if deadline_seconds <= 0:
        raise ValueError("generation deadline must be positive")
    deadline_at = clock() + deadline_seconds
    evidence_executed = False

    def complete(task):
        prompt = json.dumps(
            {
                "query": context.get("query", ""),
                "contexts": [item["content"] for item in context.get("results", [])],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        value = llm_complete(
            ports.llm,
            task=task,
            prompt=prompt,
            settings=settings,
            deadline_at=deadline_at,
            clock=clock,
            sleep=context.get("sleep", time.sleep),
            jitter=context.get("jitter", random.uniform),
            breaker=context.get("llm_breaker"),
            generation_runner=context.get("generation_runner"),
        ).strip()
        if clock() >= deadline_at:
            raise GenerationDeadlineExceeded
        return value

    def general():
        return complete("generation")

    try:
        gate = context.get("gate", "pass")
        question_type = context.get("question_type", "explanatory")
        detail = {"branch": "general_generation"}
        if gate == "soft_fail":
            evidence_executed = True
            try:
                evidence = complete("evidence_extraction")
            except SearchUnavailable:
                answer = general()
                detail["fallback"] = "general_generation"
            else:
                answer = evidence or str(settings.get("not_found_message", ""))
                detail = {
                    "branch": "evidence_extraction",
                    "rescued": bool(evidence),
                }
        elif bool(settings.get("exact_citation_enabled", True)) and question_type == "regulatory":
            evidence_executed = True
            try:
                evidence = complete("evidence_extraction")
            except SearchUnavailable:
                evidence = ""
            if evidence:
                answer = evidence
                detail = {"branch": "evidence_extraction"}
            else:
                answer = general()
                detail["fallback"] = "general_generation"
        elif question_type == "extraction":
            try:
                answer = complete("short_answer_extraction")
            except SearchUnavailable:
                answer = ""
            if answer:
                detail = {"branch": "short_answer_extraction"}
            else:
                answer = general()
                detail["fallback"] = "general_generation"
        else:
            answer = general()
        context["answer"] = answer
        return StepOutcome(detail=detail, extra_trace_executed=evidence_executed)
    except GenerationDeadlineExceeded:
        context["answer"] = ""
        context["generation_deadline_exceeded"] = True
        context["cacheable"] = False
        return StepOutcome(
            passed=False,
            detail={"deadline_seconds": deadline_seconds},
            extra_trace_executed=evidence_executed,
        )
