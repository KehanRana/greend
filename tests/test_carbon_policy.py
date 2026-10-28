from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from greenroute.contracts import JobPolicy
from greenroute.grid import Snapshot
from greenroute.policy import carbon_decision, interactive_decision

NOW = datetime(2026,10,28,tzinfo=timezone.utc)


def endpoints():
    return [dict(id=region,region=region,model='demo-model',model_version='fixture-v1',
        locality='simulated',healthy=True,latency_rank=i,grid_zone=zone) for i,(region,zone) in enumerate([
        ('us-east-1','demo:virginia'),('eu-central-1','demo:frankfurt'),('ap-southeast-2','demo:sydney')])]


def signals(values=(400,100,500),age=0):
    return {e['grid_zone']:Snapshot(f"snap-{e['id']}",e['grid_zone'],v,
        NOW-timedelta(seconds=age),NOW,'fixture') for e,v in zip(endpoints(),values)}


def policy(**changes):
    return JobPolicy.model_validate(dict(allowed_regions=[e['region'] for e in endpoints()],
        max_carbon_gco2e_kwh=150,deadline_at=NOW+timedelta(seconds=10),deadline_policy='expire') | changes,
        context={'now':NOW})


def decide(p=None, eps=None, snapshots=None, now=NOW):
    return carbon_decision('request','demo-model',endpoints() if eps is None else eps,
        p or policy(), signals() if snapshots is None else snapshots, now)


def test_cleanest_region_and_trace_evidence():
    d=decide()
    assert (d.action,d.selected_endpoint,d.reason) == ('select','eu-central-1','lowest_carbon_endpoint')
    assert len(d.snapshot_ids) == len(d.eligible) == 3
    assert d.evaluated_at == NOW and d.policy_version == 'carbon-v1'
    assert all(s == 'fresh' for s in d.signal_statuses.values())
    assert interactive_decision('request','demo-model',endpoints()).selected_endpoint == 'us-east-1'


@pytest.mark.parametrize('patch,reason',[
    ({'healthy':False},'unhealthy'),({'model':'other'},'model_mismatch'),
    ({'model_version':'other'},'model_mismatch'),({'locality':'unverified'},'unapproved_locality')])
def test_endpoint_eligibility_before_carbon(patch,reason):
    eps=endpoints()
    eps[1].update(patch)
    d=decide(eps=eps)
    assert d.action == 'wait' and d.excluded['eu-central-1'] == reason
    # No geographic or capability fallback is allowed even at a permissive deadline.
    p=policy(allowed_regions=['eu-central-1'],deadline_policy='execute_best_available')
    assert decide(p,eps,now=NOW+timedelta(seconds=10)).action == 'expire'


def test_allowed_regions_cannot_be_bypassed():
    d=decide(policy(allowed_regions=['us-east-1']))
    assert d.action == 'wait' and d.reason == 'threshold_not_met'
    assert d.excluded['eu-central-1'] == 'region_not_allowed'
    d=decide(policy(allowed_regions=['missing']))
    assert d.action == 'wait' and d.reason == 'no_eligible_endpoint'


def test_threshold_equality_zero_and_no_threshold():
    assert decide(policy(max_carbon_gco2e_kwh=100)).selected_endpoint == 'eu-central-1'
    assert decide(policy(max_carbon_gco2e_kwh=99)).reason == 'threshold_not_met'
    assert decide(policy(max_carbon_gco2e_kwh=None),snapshots=signals((900,800,700))).selected_endpoint == 'ap-southeast-2'
    assert decide(policy(max_carbon_gco2e_kwh=0),snapshots=signals((400,0,500))).action == 'select'


def test_ties_use_latency_then_stable_id_independent_of_input_order():
    eps=endpoints()
    for e in eps:
        e['latency_rank']=1
    assert decide(eps=list(reversed(eps)),snapshots=signals((100,100,100))).selected_endpoint == 'ap-southeast-2'
    assert decide(snapshots=signals((100,100,100))).selected_endpoint == 'us-east-1'


@pytest.mark.parametrize('kind',['stale','missing','invalid','future','unmapped'])
def test_unusable_signals_do_not_qualify(kind):
    snapshots=signals()
    eps=endpoints()
    if kind == 'stale':
        snapshots=signals(age=301)
    elif kind == 'missing':
        snapshots={}
    elif kind == 'invalid':
        snapshots={k:replace(v,intensity=float('nan')) for k,v in snapshots.items()}
    elif kind == 'future':
        snapshots={k:replace(v,observed_at=NOW+timedelta(seconds=1)) for k,v in snapshots.items()}
    else:
        eps=[e | {'grid_zone':None} for e in eps]
    d=decide(eps=eps,snapshots=snapshots)
    assert d.action == 'wait' and d.reason == 'no_fresh_signal'


def test_only_fresh_qualifying_regions_compete():
    snapshots=signals((100,10,50))
    snapshots['demo:frankfurt']=replace(snapshots['demo:frankfurt'],observed_at=NOW-timedelta(seconds=301))
    assert decide(snapshots=snapshots).selected_endpoint == 'ap-southeast-2'


@pytest.mark.parametrize('offset,expected',[(9.999,'select'),(10,'expire'),(10.001,'expire')])
def test_expire_deadline_boundaries(offset,expected):
    d=decide(now=NOW+timedelta(seconds=offset))
    assert d.action == expected
    if expected == 'expire':
        assert d.selected_endpoint is None


@pytest.mark.parametrize('offset,action',[(9.999,'wait'),(10,'select'),(10.001,'select')])
def test_best_available_deadline_threshold_override(offset,action):
    d=decide(policy(max_carbon_gco2e_kwh=50,deadline_policy='execute_best_available'),
             now=NOW+timedelta(seconds=offset))
    assert d.action == action
    if action == 'select':
        assert d.selected_endpoint == 'eu-central-1'
        assert d.exceptions == ['threshold_exceeded']
        assert d.reason == 'deadline_best_available'


def test_deadline_prefers_fresh_then_latency_if_all_unavailable():
    p=policy(deadline_policy='execute_best_available')
    snapshots=signals((500,100,700))
    snapshots.pop('demo:frankfurt')
    d=decide(p,snapshots=snapshots,now=NOW+timedelta(seconds=10))
    assert d.selected_endpoint == 'us-east-1' and d.exceptions == ['threshold_exceeded']
    d=decide(p,snapshots={},now=NOW+timedelta(seconds=10))
    assert d.selected_endpoint == 'us-east-1'
    assert d.exceptions == ['fresh_signal_unavailable','threshold_unverifiable']
    assert decide(p,eps=[],now=NOW+timedelta(seconds=10)).action == 'expire'


def test_deadline_meeting_threshold_has_no_exception():
    d=decide(policy(deadline_policy='execute_best_available'),now=NOW+timedelta(seconds=10))
    assert d.selected_endpoint == 'eu-central-1' and d.exceptions == []


def test_mismatched_snapshot_zone_is_not_used():
    snapshots=signals()
    snapshots['demo:frankfurt']=replace(snapshots['demo:frankfurt'],zone='wrong-zone')
    d=decide(snapshots=snapshots)
    assert d.action == 'wait' and d.signal_statuses['eu-central-1'] == 'invalid'
