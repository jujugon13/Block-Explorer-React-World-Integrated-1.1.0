"""Remove candidates that fail a live permission-ledger decision."""

from src.search.core import StepOutcome


def handle(context):
    ports = context.get("ports")
    if ports is None:
        return None
    results = context.get("results", [])
    readable = ports.permissions.live_readable_document_ids(
        context["principal"], [result["document_id"] for result in results]
    )
    kept = [result for result in results if result["document_id"] in readable]
    context["results"] = kept
    return StepOutcome(detail={"removed": len(results) - len(kept)})
