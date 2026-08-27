"""Keep chunks from the highest-scoring documents in original order."""


def handle(context):
    results = context.get("results")
    if not isinstance(results, list):
        return None
    if any(
        not isinstance(item, dict)
        or "document_id" not in item
        or "score" not in item
        for item in results
    ):
        return None
    top_n = int(context["settings"].get("document_scope_top_n", 3))
    scores = {}
    first = {}
    for position, result in enumerate(results):
        document_id = result["document_id"]
        first.setdefault(document_id, position)
        scores[document_id] = max(
            scores.get(document_id, float("-inf")), float(result["score"])
        )
    selected = {
        document_id
        for document_id, _ in sorted(
            scores.items(), key=lambda item: (-item[1], first[item[0]])
        )[:top_n]
    }
    context["results"] = [
        result for result in results if result["document_id"] in selected
    ]
    return None
