import asyncio
import copy
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from greenroute.app import create_app
from greenroute.contracts import AdapterResult
from greenroute.estimation import calculate,capture_context,metrics,persist
from greenroute.grid import latest_snapshots
from greenroute.jobs import stamp
from test_jobs import env,add,read,worker,run_cycle,completion,NOW,KEY,HEADERS


def context(env):
    with env[1].connect() as conn:
        endpoints=list(conn.execute(text('SELECT * FROM endpoints')).mappings())
    return capture_context(env[0],'demo-model',endpoints,[e['id'] for e in endpoints],latest_snapshots(env[1]),NOW)


def success(engine,id,ctx,usage=None):
    decision={'mode':ctx['mode'] if ctx else 'demo','selected_endpoint':'eu-central-1','estimation_context':ctx}
    usage=usage if usage is not None else {'prompt_tokens':1000,'completion_tokens':1000,'total_tokens':2000}
    with engine.begin() as conn:
        conn.execute(text('INSERT INTO decisions (request_id,created_at,data) VALUES (:id,:at,:data)'),
                     dict(id=id,at=stamp(NOW),data=json.dumps(decision)))
        conn.execute(text("INSERT INTO attempts (id,request_id,endpoint_id,started_at,ended_at,outcome,uncertain,usage) "
                          "VALUES (:id,:request,'eu-central-1',:at,:at,'succeeded',0,:usage)"),
                     dict(id='attempt_'+id,request=id,at=stamp(NOW),usage=json.dumps(usage)))
        persist(conn,'attempt_'+id,decision,usage,NOW)
    return decision


def test_known_arithmetic_and_secondary_comparison(env):
    ctx=context(env)
    ctx['signals']['eu-central-1']['intensity']=100
    result=calculate(ctx,'eu-central-1',{'prompt_tokens':1000,'completion_tokens':1000})
    assert result['energy_kwh']==.002 and result['baseline_g']==.8
    assert result['actual_g']==.2 and result['difference_g']==.6
    assert result['worst_region_comparison']==dict(endpoint='ap-southeast-2',baseline_g=1.0,difference_g=.8)
    ctx['signals']['eu-central-1']['intensity']=600
    assert calculate(ctx,'eu-central-1',{'prompt_tokens':1000,'completion_tokens':1000})['difference_g']==-.4


@pytest.mark.parametrize('usage,reason',[(None,'missing_token_usage'),({},'missing_token_usage'),
    ({'prompt_tokens':1},'missing_token_usage'),({'prompt_tokens':-1,'completion_tokens':2},'missing_token_usage'),
    ({'prompt_tokens':True,'completion_tokens':2},'missing_token_usage'),
    ({'prompt_tokens':1,'completion_tokens':2,'total_tokens':99},'inconsistent_token_usage')])
def test_missing_or_inconsistent_tokens_are_not_measured_zero(env,usage,reason):
    result=calculate(context(env),'eu-central-1',usage)
    assert result['status']=='unavailable' and result['reason']==reason and result['difference_g'] is None


@pytest.mark.parametrize('change,reason',[
    ('baseline_ineligible','baseline_ineligible'),('baseline_stale','baseline_signal_unavailable'),
    ('selected_missing','selected_signal_unavailable'),('selected_stale','selected_signal_unavailable'),
    ('mode_mismatch','mixed_signal_modes')])
def test_unavailable_comparison_preserves_energy(env,change,reason):
    ctx=context(env)
    if change=='baseline_ineligible':ctx['eligible'].remove('us-east-1')
    elif change=='baseline_stale':ctx['signals']['us-east-1']['status']='stale'
    elif change=='selected_missing':ctx['signals']['eu-central-1']=None
    elif change=='selected_stale':ctx['signals']['eu-central-1']['status']='stale'
    else:ctx['signals']['eu-central-1']['mode']='live'
    result=calculate(ctx,'eu-central-1',{'prompt_tokens':1000,'completion_tokens':1000})
    assert result['reason']==reason and result['difference_g'] is None and result['energy_kwh']==.002


def test_dispatch_assumptions_are_immutable_and_deduplicated(env):
    jid=add(env,threshold=None)
    w=worker(env)
    claim=w.claim(jid)
    w.settings=w.settings.model_copy(update={'energy_kwh_per_1000_tokens':.5,'baseline_endpoint':'ap-southeast-2'})
    result=AdapterResult(completion=completion(),usage={'prompt_tokens':1000,'completion_tokens':1000,'total_tokens':2000})
    assert w.finish(claim,result=result)
    with env[1].begin() as conn:
        raw=conn.execute(text('SELECT * FROM emissions_estimates')).mappings().one()
        assumptions=json.loads(raw['assumptions'])
        assert assumptions['kwh_per_1000_tokens']==.001 and raw['baseline_endpoint']=='us-east-1'
        assert raw['difference_g']==.2
        changed=read(env,jid)['decision']
        changed['estimation_context']['kwh_per_1000_tokens']=.8
        persist(conn,claim['attempt'],changed,result.usage,NOW)
    with env[1].connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM emissions_estimates')).scalar()==1
        assert conn.execute(text('SELECT difference_g FROM emissions_estimates')).scalar()==.2


def test_aggregates_separate_modes_versions_exclusions_and_zero(env):
    engine=env[1]
    ctx=context(env)
    success(engine,'first',ctx)
    another=copy.deepcopy(ctx)
    another['energy_model_version']='illustrative-v2'
    another['signals']['eu-central-1']['intensity']=500
    success(engine,'negative',another)
    missing=copy.deepcopy(ctx)
    missing['signals']['us-east-1']['status']='stale'
    success(engine,'stale',missing)
    success(engine,'zero',ctx,{'prompt_tokens':0,'completion_tokens':0})
    live=copy.deepcopy(ctx);live['mode']='live'
    for s in live['signals'].values():s['mode']='live'
    success(engine,'live',live)
    success(engine,'historical',None)
    data=metrics(engine,'local','demo',NOW)
    assert data['succeeded_executions']==5 and data['compared_executions']==3 and data['excluded_executions']==2
    assert data['estimated_baseline_g']==1.6 and data['estimated_actual_g']==1.6
    assert data['estimated_difference_g']==0 and data['compared_tokens']==4000
    assert data['estimated_difference_g_per_1000_tokens']==0 and len(data['estimation_groups'])==2
    assert data['excluded_reasons']=={'baseline_signal_unavailable':1,'missing_dispatch_evidence':1}
    assert metrics(engine,'local','live',NOW)['succeeded_executions']==1


def test_metrics_auth_empty_and_synchronous_persistence(env):
    settings,_,clock,_=env
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json=completion()))
    with TestClient(create_app(settings,transport,clock)) as client:
        assert client.get('/api/metrics').status_code==401
        data=client.get('/api/metrics',headers=HEADERS).json()
        assert data['estimated_difference_g']==0 and data['estimated_difference_g_per_1000_tokens'] is None
        assert data['compared_executions']==0
        response=client.post('/v1/chat/completions',headers=HEADERS,
            json={'model':'demo-model','messages':[{'role':'user','content':'PRIVATE INPUT'}]})
        assert response.status_code==200
        item=client.get('/api/activity',headers=HEADERS).json()['items'][0]
        assert item['estimate']['status']=='available' and item['estimate']['difference_g']==0
        assert item['estimate']['usage_provenance']=='simulated_usage'
        assert 'PRIVATE' not in json.dumps(item)
        assert client.get('/api/metrics?mode=live',headers=HEADERS).json()['compared_executions']==0


def test_scenario_auth_release_without_reset_and_restart(env):
    settings,engine,clock,_=env
    operator={'authorization':'Bearer operator-only-key'}
    with TestClient(create_app(settings,clock=clock)) as client:
        assert client.post('/api/demo/scenario',headers=HEADERS,json={'scenario':'clean'}).status_code==401
        assert client.post('/api/demo/scenario',headers=operator,json={'scenario':'invalid'}).status_code==422
        assert client.post('/api/demo/scenario',headers=operator,json={'scenario':'high'}).json()['refreshed']
        jid=add(env)
        asyncio.run(run_cycle(env,lambda r:httpx.Response(200,json=completion())))
        assert read(env,jid)['status']=='waiting_for_carbon'
        revision=client.get('/api/demo/scenario',headers=HEADERS).json()['revision']
    clock.advance(301)
    with TestClient(create_app(settings,clock=clock)) as client:
        assert client.get('/api/demo/scenario',headers=HEADERS).json()['revision']==revision
        regions=client.get('/api/regions',headers=HEADERS).json()['items']
        assert next(r for r in regions if r['region']=='eu-central-1')['intensity_gco2e_kwh']==300
        assert client.post('/api/demo/scenario',headers=operator,json={'scenario':'clean'}).status_code==200
        asyncio.run(run_cycle(env,lambda r:httpx.Response(200,json=completion())))
        assert read(env,jid)['status']=='succeeded'
        data=client.get('/api/metrics',headers=HEADERS).json()
        assert data['succeeded_executions']==data['compared_executions']==1
        assert data['estimated_difference_g']==.0048


def test_live_mode_has_no_scenario_mutations(env):
    settings,_,clock,_=env
    with TestClient(create_app(settings.model_copy(update={'mode':'live'}),clock=clock)) as client:
        assert client.post('/api/demo/scenario',headers={'authorization':'Bearer operator-only-key'},json={'scenario':'clean'}).status_code==404
