"""Intersect indexed documents with the requester's readable set."""

from src.search.core import StepOutcome


def handle(context):
    ports = context.get("ports")
    if ports is None:
        return None
    indexed = ports.index_catalog.indexed_document_ids()
    allowed = ports.permissions.readable_document_ids(context["principal"], indexed)
    context["allowed_document_ids"] = frozenset(allowed)
    if allowed:
        return StepOutcome(detail={"document_count": len(allowed)})
    context["results"] = []
    context["answer"] = ""
    return StepOutcome(detail={"document_count": 0}, terminate=True)
