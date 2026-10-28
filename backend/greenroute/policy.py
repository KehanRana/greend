"""Pure interactive policy; carbon-aware decisions are planned for M2."""
from greenroute.contracts import DecisionRecord


def interactive_decision(request_id, model, endpoints):
    ordered = sorted(endpoints, key=lambda e: (e["latency_rank"], e["id"]))
    eligible, excluded = [], {}
    for e in ordered:
        if not e["healthy"]:
            excluded[e["id"]] = "unhealthy"
        elif e["model"] != model or e["model_version"] != "fixture-v1":
            excluded[e["id"]] = "model_mismatch"
        elif e["locality"] != "simulated":
            excluded[e["id"]] = "unapproved_locality"
        else:
            eligible.append(e["id"])
    return DecisionRecord(request_id=request_id, selected_endpoint=eligible[0] if eligible else None,
                          eligible=eligible, excluded=excluded,
                          reason="preferred_healthy_endpoint" if eligible else "no_eligible_endpoint")
