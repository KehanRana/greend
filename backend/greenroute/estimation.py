"""Versioned operational estimates from immutable dispatch evidence, not live config."""
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
from uuid import uuid4

from sqlalchemy import text
from greenroute.grid import signal_status
from greenroute.jobs import stamp

METHOD = 'total-token-grid-v1'


def capture_context(settings, model, endpoints, eligible, snapshots, now):
    evidence = {}
    for e in endpoints:
        if e['id'] not in eligible:
            continue
        snapshot=snapshots.get(e['grid_zone']) if e.get('grid_zone') else None
        evidence[e['id']] = None if snapshot is None else dict(id=snapshot.id,zone=snapshot.zone,
            intensity=snapshot.intensity,source=snapshot.source,mode=snapshot.mode,
            temporal_granularity=snapshot.temporal_granularity,
            observed_at=snapshot.observed_at.isoformat(),retrieved_at=snapshot.retrieved_at.isoformat(),
            status=signal_status(snapshot,now,settings.grid_max_age_seconds,settings.mode,
                                 settings.grid_temporal_granularity if settings.mode == "live" else None))
    return dict(method_version=METHOD,bound_at=stamp(now),model=model,model_version=next((e['model_version'] for e in endpoints if e['id'] in eligible),None),
        baseline_endpoint=settings.baseline_endpoint,eligible=list(eligible),signals=evidence,
        energy_model_version=settings.energy_model_version,
        kwh_per_1000_tokens=settings.energy_kwh_per_1000_tokens,
        grid_temporal_granularity=settings.grid_temporal_granularity if settings.mode == "live" else None,
        grid_max_age_seconds=settings.grid_max_age_seconds,energy_is_illustrative=True,usage_provenance='simulated_usage' if settings.mode == 'demo' else 'provider_reported_usage',mode=settings.mode)


def calculate(context, selected, usage):
    result=dict(status='unavailable',reason=None,tokens=None,energy_kwh=None,actual_g=None,baseline_g=None,
                difference_g=None,worst_region_comparison=None)
    if not context or context.get('method_version') != METHOD:
        return result | {'reason':'missing_dispatch_evidence'}
    if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0
                                       for k in ('prompt_tokens','completion_tokens')):
        return result | {'reason':'missing_token_usage'}
    tokens=usage['prompt_tokens']+usage['completion_tokens']
    if 'total_tokens' in usage and (type(usage['total_tokens']) is not int or usage['total_tokens'] != tokens):
        return result | {'reason':'inconsistent_token_usage'}
    result['tokens']=tokens
    coefficient=Decimal(str(context['kwh_per_1000_tokens']))
    energy=Decimal(tokens)/1000*coefficient
    result['energy_kwh']=float(energy)
    baseline=context['baseline_endpoint']
    signals=context['signals']
    if not signals.get(selected) or signals[selected]['status'] != 'fresh':
        return result | {'reason':'selected_signal_unavailable'}
    if signals[selected]['mode'] != context['mode']:
        return result | {'reason':'mixed_signal_modes'}
    def rounded(value):
        return value.quantize(Decimal('0.000000001'),rounding=ROUND_HALF_EVEN)
    actual=rounded(energy*Decimal(str(signals[selected]['intensity'])))
    result['actual_g']=float(actual)
    if baseline not in context['eligible']:
        return result | {'reason':'baseline_ineligible'}
    if not signals.get(baseline) or signals[baseline]['status'] != 'fresh':
        return result | {'reason':'baseline_signal_unavailable'}
    if signals[baseline]['mode'] != context['mode']:
        return result | {'reason':'mixed_signal_modes'}
    reference=rounded(energy*Decimal(str(signals[baseline]['intensity'])))
    fresh=[(s['intensity'],eid) for eid,s in signals.items() if s and s['status']=='fresh' and s['mode']==context['mode']]
    if fresh:
        worst_intensity,worst_id=max(fresh)
        worst=rounded(energy*Decimal(str(worst_intensity)))
        result['worst_region_comparison']=dict(endpoint=worst_id,baseline_g=float(worst),difference_g=float(worst-actual))
    return result | dict(status='available',reason=None,actual_g=float(actual),baseline_g=float(reference),difference_g=float(reference-actual))


def persist(conn, execution_id, decision, usage, now):
    """Called in the successful attempt's transaction. Unique execution identity deduplicates."""
    context=decision.get('estimation_context')
    selected=decision.get('selected_endpoint')
    estimate=calculate(context,selected,usage)
    assumptions=dict(context or {})
    assumptions['worst_region_comparison']=estimate['worst_region_comparison']
    conn.execute(text('INSERT INTO emissions_estimates (id,execution_id,method_version,assumptions,energy_kwh,'
        'actual_g,baseline_g,difference_g,status,reason,created_at,mode,tokens,usage_provenance,baseline_endpoint,selected_endpoint) '
        'VALUES (:id,:execution,:method,:assumptions,:energy,:actual,:baseline,:difference,:status,:reason,:at,:mode,:tokens,'
        ':provenance,:baseline_endpoint,:selected) ON CONFLICT(execution_id) DO NOTHING'),
        dict(id='estimate_'+uuid4().hex,execution=execution_id,method=(context or {}).get('method_version',METHOD),
             assumptions=json.dumps(assumptions),energy=estimate['energy_kwh'],actual=estimate['actual_g'],
             baseline=estimate['baseline_g'],difference=estimate['difference_g'],status=estimate['status'],reason=estimate['reason'],
             at=stamp(now),mode=(context or {}).get('mode','demo'),tokens=estimate['tokens'],
             provenance=(context or {}).get('usage_provenance','unavailable'),
             baseline_endpoint=(context or {}).get('baseline_endpoint'),selected=selected))


def public_estimate(row):
    if not row or not row['id']:
        return None
    return dict(id=row['id'],execution_id=row['execution_id'],status=row['status'],reason=row['reason'],
        method_version=row['method_version'],assumptions=json.loads(row['assumptions'] or '{}'),
        energy_kwh=row['energy_kwh'],actual_g=row['actual_g'],baseline_g=row['baseline_g'],difference_g=row['difference_g'],
        tokens=row['tokens'],usage_provenance=row['usage_provenance'],baseline_endpoint=row['baseline_endpoint'],
        selected_endpoint=row['selected_endpoint'],mode=row['mode'],created_at=row['created_at'])


def metrics(engine, owner, mode, now, runtime_mode="demo"):
    with engine.connect() as conn:
        rows=list(conn.execute(text("SELECT a.id AS attempt_id,a.started_at,e.* FROM attempts a "
            "JOIN decisions d ON d.request_id=a.request_id LEFT JOIN emissions_estimates e ON e.execution_id=a.id "
            "WHERE a.outcome='succeeded' AND COALESCE(json_extract(d.data,'$.mode'),'demo')=:mode"),dict(mode=mode)).mappings())
        queue=dict(conn.execute(text('SELECT state,COUNT(*) FROM jobs WHERE owner_id=:owner GROUP BY state'),dict(owner=owner)).all())
        outcomes=dict(conn.execute(text("SELECT a.outcome,COUNT(*) FROM attempts a JOIN decisions d ON d.request_id=a.request_id "
            "WHERE COALESCE(json_extract(d.data,'$.mode'),'demo')=:mode GROUP BY a.outcome"),dict(mode=mode)).all())
    if mode != runtime_mode:
        queue={}
    available=[r for r in rows if r['status']=='available' and r['mode']==mode]
    def total(field):
        return sum((Decimal(str(r[field])) for r in available),Decimal(0)).quantize(Decimal('0.000001'))
    actual,baseline=total('actual_g'),total('baseline_g')
    token_sum=sum(r['tokens'] for r in available)
    included={r['attempt_id'] for r in available}
    excluded=Counter(r['reason'] or 'missing_dispatch_evidence' for r in rows if r['attempt_id'] not in included)
    groups={}
    for r in available:
        context=json.loads(r['assumptions'])
        key=(r['baseline_endpoint'],context.get('energy_model_version'),context.get('kwh_per_1000_tokens'),context.get('model_version'))
        if key not in groups:
            groups[key]=dict(baseline_endpoint=key[0],energy_model_version=key[1],kwh_per_1000_tokens=key[2],
                             model_version=key[3],executions=0)
        groups[key]['executions']+=1
    return dict(mode=mode,evaluated_at=stamp(now),scope='retained workspace executions',
        succeeded_executions=len(rows),compared_executions=len(available),excluded_executions=len(rows)-len(available),
        excluded_reasons=dict(excluded),estimated_actual_g=float(actual),estimated_baseline_g=float(baseline),
        estimated_difference_g=float(baseline-actual),compared_tokens=token_sum,
        estimated_difference_g_per_1000_tokens=float((baseline-actual)*1000/token_sum) if token_sum else None,
        estimation_groups=list(groups.values()),outcomes=outcomes,owner_job_states=queue,
        first_execution_at=min((r['started_at'] for r in rows),default=None),
        note=('Simulated' if mode == 'demo' else 'Illustrative') + ' operational estimates; excludes failed attempts, networking, cooling and embodied impacts. No measured avoided emissions.')
