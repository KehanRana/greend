"""Pure interactive and flexible-work policies; no database or network I/O."""
from greenroute.contracts import DecisionRecord


def interactive_decision(request_id, model, endpoints, mode="demo", model_version="fixture-v1"):
    ordered = sorted(endpoints, key=lambda e: (e["latency_rank"], e["id"]))
    eligible, excluded = [], {}
    for e in ordered:
        if not e["healthy"]:
            excluded[e["id"]] = "unhealthy"
        elif e["model"] != model or e["model_version"] != model_version:
            excluded[e["id"]] = "model_mismatch"
        elif e["locality"] != ("simulated" if mode == "demo" else "verified"):
            excluded[e["id"]] = "unapproved_locality"
        else:
            eligible.append(e["id"])
    return DecisionRecord(mode=mode, request_id=request_id, selected_endpoint=eligible[0] if eligible else None,
                          eligible=eligible, excluded=excluded,
                          reason="preferred_healthy_endpoint" if eligible else "no_eligible_endpoint")


def carbon_decision(request_id, model, endpoints, policy, snapshots, now, max_age_seconds=300,
                    mode="demo", model_version="fixture-v1", granularity=None):
    """Pure flexible-work decision. The caller dispatches or persists the result in M3.

    JobPolicy is validated at submission; its deadline is compared to the injected
    evaluation time here rather than revalidated after the job has waited.
    """
    from greenroute.clock import require_aware
    from greenroute.contracts import CarbonDecision
    from greenroute.grid import signal_status

    now = require_aware(now)
    deadline = require_aware(policy.deadline_at)
    ordered = sorted(endpoints, key=lambda e: (e["latency_rank"], e["id"]))
    eligible, excluded = [], {}
    statuses, snapshot_ids = {}, {}
    for endpoint in ordered:
        eid = endpoint["id"]
        if endpoint["region"] not in policy.allowed_regions:
            excluded[eid] = "region_not_allowed"
        elif not endpoint["healthy"]:
            excluded[eid] = "unhealthy"
        elif endpoint["model"] != model or endpoint["model_version"] != model_version:
            excluded[eid] = "model_mismatch"
        elif endpoint["locality"] != ("simulated" if mode == "demo" else "verified"):
            excluded[eid] = "unapproved_locality"
        else:
            eligible.append(endpoint)
            zone = endpoint.get("grid_zone")
            snapshot = snapshots.get(zone)
            statuses[eid] = ("invalid" if snapshot and snapshot.zone != zone
                             else signal_status(snapshot, now, max_age_seconds, mode, granularity))
            if snapshot:
                snapshot_ids[eid] = snapshot.id

    def decision(action, reason, selected=None, exceptions=()):
        return CarbonDecision(mode=mode, request_id=request_id, action=action, reason=reason,
            selected_endpoint=selected["id"] if selected else None,
            eligible=[e["id"] for e in eligible], excluded=excluded,
            signal_statuses=statuses, snapshot_ids=snapshot_ids,
            exceptions=list(exceptions), evaluated_at=now)

    reached_deadline = now >= deadline
    if reached_deadline and policy.deadline_policy == "expire":
        return decision("expire", "deadline_expired")
    if not eligible:
        return decision("expire" if reached_deadline else "wait", "no_eligible_endpoint")
    fresh = [e for e in eligible if statuses[e["id"]] == "fresh"]
    threshold = policy.max_carbon_gco2e_kwh

    def carbon_rank(endpoint):
        return (snapshots[endpoint["grid_zone"]].intensity, endpoint["latency_rank"], endpoint["id"])

    if reached_deadline:
        if fresh:
            selected = min(fresh, key=carbon_rank)
            exceptions = ["threshold_exceeded"] if (threshold is not None and
                snapshots[selected["grid_zone"]].intensity > threshold) else []
        else:
            selected = eligible[0]
            exceptions = ["fresh_signal_unavailable"]
            if threshold is not None:
                exceptions.append("threshold_unverifiable")
        return decision("select", "deadline_best_available", selected, exceptions)
    if not fresh:
        return decision("wait", "no_fresh_signal")
    qualifying = [e for e in fresh if threshold is None or snapshots[e["grid_zone"]].intensity <= threshold]
    if not qualifying:
        return decision("wait", "threshold_not_met")
    return decision("select", "lowest_carbon_endpoint", min(qualifying, key=carbon_rank))
