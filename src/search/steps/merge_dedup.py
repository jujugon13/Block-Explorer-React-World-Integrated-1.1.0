"""Merge query batches and keep the highest score for each chunk."""


def handle(context):
    batches = context.get("search_batches")
    if batches is None:
        return None
    best = {}
    for batch in batches:
        for result in batch:
            chunk_id = result["chunk_id"]
            previous = best.get(chunk_id)
            if previous is None or float(result["score"]) > float(previous["score"]):
                best[chunk_id] = dict(result)
    context["results"] = sorted(
        best.values(), key=lambda item: float(item["score"]), reverse=True
    )
    return None
