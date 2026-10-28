import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from greenroute import jobs
from greenroute.app import create_app
from greenroute.clock import ManualClock
from greenroute.config import Settings
from greenroute.contracts import AdapterResult, JobSubmission
from greenroute.grid import FixtureGridAdapter, GridPoller
from greenroute.store import make_engine
from greenroute.worker import Worker

NOW=datetime(2026,10,28,tzinfo=timezone.utc)
KEY='jobs-local-key'
HEADERS={'authorization':f'Bearer {KEY}','idempotency-key':'sample-key'}


def body(deadline=600,threshold=150,deadline_policy='expire'):
    return {'request':{'model':'demo-model','messages':[{'role':'user','content':'PRIVATE JOB INPUT'}]},
            'policy':{'allowed_regions':['us-east-1','eu-central-1','ap-southeast-2'],
                      'max_carbon_gco2e_kwh':threshold,'deadline_at':(NOW+timedelta(seconds=deadline)).isoformat(),
                      'deadline_policy':deadline_policy}}


def completion():
    return {'id':'chatcmpl-job','object':'chat.completion','created':1,'model':'demo-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':'PRIVATE JOB OUTPUT'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}


@pytest.fixture
def env(tmp_path,monkeypatch):
    path=tmp_path/'jobs.db'
    monkeypatch.setenv('GREENROUTE_DATABASE',str(path))
    monkeypatch.setenv('GREENROUTE_GATEWAY_KEY',KEY)
    command.upgrade(Config('alembic.ini'),'head')
    engine=make_engine(path)
    clock=ManualClock(NOW)
    settings=Settings(gateway_key=KEY,database=path,client_keys={'other':'another-client-key'},
                      operator_key='operator-only-key')
    poller=GridPoller(engine,FixtureGridAdapter(clock),clock)
    assert asyncio.run(poller.refresh())
    yield settings,engine,clock,poller
    engine.dispose()


def add(env,key='key',**kwargs):
    settings,engine,clock,_=env
    return jobs.submit(engine,'local',key,JobSubmission.model_validate(body(**kwargs)),clock.now(),settings.pending_job_limit)[0]


def read(env,jid,owner='local'):
    return jobs.get_job(env[1],owner,jid,env[2].now())


def worker(env,client=None):
    return Worker(env[1],env[0],client,env[2])


async def run_cycle(env,handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        w=worker(env,client)
        await w.tick()
        await w.drain()


def test_api_idempotency_deadline_and_owner_isolation(env):
    settings,engine,clock,_=env
    with TestClient(create_app(settings,clock=clock)) as client:
        assert client.post('/api/jobs',json=body()).status_code == 401
        assert client.post('/api/jobs',json=body(),headers={'authorization':HEADERS['authorization']}).status_code == 422
        response=client.post('/api/jobs',json=body(deadline=10),headers=HEADERS)
        assert response.status_code == 202
        jid=response.json()['id']
        assert client.post('/api/jobs',json=body(deadline=10),headers=HEADERS).json()['id'] == jid
        assert client.post('/api/jobs',json=body(),headers=HEADERS).status_code == 409
        other={'authorization':'Bearer another-client-key'}
        assert client.get(f'/api/jobs/{jid}',headers=other).status_code == 404
        assert client.post(f'/api/jobs/{jid}/cancel',headers=other).status_code == 404
        assert client.get('/api/jobs',headers=other).json()['items'] == []
        detail=client.get(f'/api/jobs/{jid}',headers=HEADERS).json()
        assert detail['status'] == 'queued' and detail['result'] is None
        assert 'PRIVATE JOB INPUT' not in json.dumps(detail)
        clock.advance(11)
        assert client.post('/api/jobs',json=body(deadline=10),headers=HEADERS).json()['id'] == jid
        for deadline in [-1,11,86412]:
            assert client.post('/api/jobs',json=body(deadline=deadline),
                              headers=HEADERS|{'idempotency-key':f'invalid-{deadline}'}).status_code == 422
        assert client.post(f'/api/jobs/{jid}/cancel',headers=HEADERS).json()['status'] == 'cancelled'
        assert client.post(f'/api/jobs/{jid}/cancel',headers=HEADERS).status_code == 409
    with TestClient(create_app(settings,clock=clock)) as restarted:
        assert restarted.get(f'/api/jobs/{jid}',headers=HEADERS).json()['status'] == 'cancelled'


def test_atomic_queue_capacity_and_duplicate_submissions(env):
    _,engine,clock,_=env
    submission=JobSubmission.model_validate(body())
    def submit(i):
        try:
            return jobs.submit(engine,'local',str(i),submission,clock.now(),3)[0]
        except jobs.JobError as e:
            return e.status
    with ThreadPoolExecutor(max_workers=10) as pool:
        results=list(pool.map(submit,range(20)))
    assert sum(isinstance(r,str) for r in results) == 3
    assert results.count(429) == 17
    accepted_key=str(next(i for i,r in enumerate(results) if isinstance(r,str)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        repeats=list(pool.map(lambda _:jobs.submit(engine,'local',accepted_key,submission,clock.now(),3)[0],range(8)))
    assert len(set(repeats)) == 1


def test_storage_failure_is_not_acknowledged(env):
    settings,engine,clock,_=env
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER reject_job BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'PRIVATE DATABASE DETAILS'); END")
    with TestClient(create_app(settings,clock=clock)) as client:
        r=client.post('/api/jobs',json=body(),headers=HEADERS)
        assert r.status_code == 503 and 'PRIVATE' not in r.text
        with engine.begin() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM jobs')).scalar() == 0
            conn.exec_driver_sql('DROP TRIGGER reject_job')
        assert client.post('/api/jobs',json=body(),headers=HEADERS).status_code == 202


def test_wait_release_completion_and_private_metadata(env):
    settings,engine,clock,poller=env
    jid=add(env)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=completion())
    asyncio.run(run_cycle(env,handler))
    assert read(env,jid)['status'] == 'waiting_for_carbon' and not calls
    clock.advance(180)
    assert asyncio.run(poller.refresh())
    asyncio.run(run_cycle(env,handler))
    detail=read(env,jid)
    assert detail['status'] == 'succeeded' and detail['result']['choices'][0]['message']['content'] == 'PRIVATE JOB OUTPUT'
    assert detail['decision']['selected_endpoint'] == 'eu-central-1' and detail['attempt_count'] == 1
    assert calls[0].headers['authorization'] == 'Bearer demo-upstream-key'
    assert jobs.submit(engine,'local','key',JobSubmission.model_validate(body()),clock.now(),1000)[0] == jid
    with TestClient(create_app(settings,clock=clock)) as client:
        for route in ['/api/activity','/api/regions','/api/jobs']:
            metadata=client.get(route,headers=HEADERS).text
            assert 'PRIVATE JOB' not in metadata and KEY not in metadata
        assert client.post(f'/api/jobs/{jid}/cancel',headers=HEADERS).status_code == 409
        assert client.get(f'/api/jobs/{jid}',headers={'authorization':'Bearer another-client-key'}).status_code == 404


@pytest.mark.parametrize('policy,expected',[('expire','expired'),('execute_best_available','succeeded')])
def test_worker_deadline_boundary(env,policy,expected):
    jid=add(env,deadline=10,deadline_policy=policy)
    asyncio.run(run_cycle(env,lambda r:httpx.Response(200,json=completion())))
    assert read(env,jid)['status'] == 'waiting_for_carbon'
    env[2].advance(10)
    asyncio.run(run_cycle(env,lambda r:httpx.Response(200,json=completion())))
    detail=read(env,jid)
    assert detail['status'] == expected
    if policy == 'execute_best_available':
        assert detail['decision']['exceptions'] == ['threshold_exceeded']


def test_cancel_claim_race_and_competing_claims(env):
    jid=add(env,threshold=None)
    barrier=Barrier(2)
    w=worker(env)
    def claim():
        barrier.wait()
        return w.claim(jid)
    def cancel():
        barrier.wait()
        try:
            return jobs.cancel(env[1],'local',jid,env[2].now())
        except jobs.JobError as e:
            return e.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=pool.submit(claim),pool.submit(cancel)
        claimed,cancelled=a.result(),b.result()
    if claimed:
        assert cancelled == 409 and read(env,jid)['status'] == 'running'
    else:
        assert cancelled['status'] == read(env,jid)['status'] == 'cancelled'
    jid2=add(env,'compete',threshold=None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:worker(env).claim(jid2),range(8)))
    assert sum(r is not None for r in results) == 1
    with env[1].connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM attempts WHERE job_id=:id'),dict(id=jid2)).scalar() == 1


def test_global_worker_limit_and_leases(env):
    settings,engine,clock,_=env
    constrained=(settings.model_copy(update={'worker_concurrency':2}),engine,clock,env[3])
    ids=[add(env,str(i),threshold=None) for i in range(10)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims=list(pool.map(lambda jid:worker(constrained).claim(jid),ids))
    assert sum(c is not None for c in claims) == 2
    claim=next(c for c in claims if c)
    w=worker(constrained)
    clock.advance(30)
    assert w.renew(claim)
    clock.advance(121)
    w.recover()
    assert read(env,claim['id'])['status'] == 'unknown'
    assert not w.renew(claim)
    assert not w.finish(claim,result=AdapterResult(completion=completion()))
    assert read(env,claim['id'])['attempts'][0]['uncertain']


def test_safe_retry_limit_and_backoff(env):
    jid=add(env,threshold=None)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={'retry-after':'2'})
    for advance,count,state in [(0,1,'retry_pending'),(0,1,'retry_pending'),(2,2,'retry_pending'),(4,3,'failed')]:
        env[2].advance(advance)
        asyncio.run(run_cycle(env,handler))
        assert len(calls) == count and read(env,jid)['status'] == state
    assert read(env,jid)['attempt_count'] == 3


@pytest.mark.parametrize('kind,expected', [('read','unknown'),('server','unknown'),('bad','unknown'),('connect','retry_pending'),('auth','failed')])
def test_adapter_failure_classification(env,kind,expected):
    jid=add(env,threshold=None)
    def handler(request):
        if kind == 'read':
            raise httpx.ReadTimeout('PRIVATE provider error',request=request)
        if kind == 'connect':
            raise httpx.ConnectError('PRIVATE provider error',request=request)
        return httpx.Response({'server':503,'bad':200,'auth':401}[kind],json={'private':'PRIVATE provider error'})
    asyncio.run(run_cycle(env,handler))
    detail=read(env,jid)
    assert detail['status'] == expected and 'PRIVATE provider' not in str(detail)
    if expected == 'unknown':
        asyncio.run(run_cycle(env,handler))
        assert read(env,jid)['attempt_count'] == 1


def test_retry_after_is_honored_and_never_retried_past_deadline(env):
    jid=add(env,threshold=None,deadline=10)
    asyncio.run(run_cycle(env,lambda r:httpx.Response(429,headers={'retry-after':'30'})))
    assert read(env,jid)['status'] == 'failed' and read(env,jid)['attempt_count'] == 1


def test_retention_active_payloads_and_key_reuse(env):
    _,engine,clock,_=env
    completed=add(env,'complete',threshold=None)
    active=add(env,'active')
    asyncio.run(run_cycle(env,lambda r:httpx.Response(200,json=completion())))
    assert read(env,completed)['status'] == 'succeeded'
    clock.advance(86400)
    jobs.cleanup(engine,clock.now())
    detail=read(env,completed)
    assert detail['result'] is None and detail['result_expired']
    with engine.connect() as conn:
        assert conn.execute(text('SELECT payload FROM jobs WHERE id=:id'),dict(id=completed)).scalar() is None
        assert conn.execute(text('SELECT payload FROM jobs WHERE id=:id'),dict(id=active)).scalar() is not None
    fresh=body(deadline=87000,threshold=None)
    newid=jobs.submit(engine,'local','complete',JobSubmission.model_validate(fresh),clock.now(),1000)[0]
    assert newid != completed
    clock.advance(6*86400)
    jobs.cleanup(engine,clock.now())
    with pytest.raises(jobs.JobError) as error:
        read(env,completed)
    assert error.value.status == 404
    assert read(env,active)['status'] == 'waiting_for_carbon'


def test_health_circuit_probe_and_operator_correction(env):
    settings,engine,clock,_=env
    ids=[add(env,f'bad-{i}',threshold=None) for i in range(3)]
    asyncio.run(run_cycle(env,lambda r:httpx.Response(503,json={'error':'PRIVATE upstream details'})))
    with engine.connect() as conn:
        row=conn.execute(text("SELECT * FROM endpoints WHERE id='eu-central-1'")).mappings().one()
    assert not row['healthy'] and row['consecutive_failures'] == 3 and row['disabled_reason'] == 'circuit_open'
    calls=[]
    async def probe():
        def handle(request):
            calls.append(request)
            return httpx.Response(200,json={'status':'ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w=worker(env,client)
            await w.probe_due()
            assert calls == []
            clock.advance(30)
            await w.probe_due()
    asyncio.run(probe())
    assert len(calls) == 1 and calls[0].url.path == '/eu-central-1/health'
    jid=add(env,'auth',threshold=None)
    asyncio.run(run_cycle(env,lambda r:httpx.Response(401)))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT disabled_reason FROM endpoints WHERE id='eu-central-1'")).scalar() == 'configuration_error'
    clock.advance(60)
    asyncio.run(worker(env).probe_due())  # No client: configuration errors must not auto-probe.
    with TestClient(create_app(settings,clock=clock)) as client:
        route='/api/operator/endpoints/eu-central-1/enable'
        assert client.post(route,headers=HEADERS).status_code == 401
        assert client.post(route,headers={'authorization':'Bearer operator-only-key'}).status_code == 200
        assert client.post('/api/operator/endpoints/missing/enable',headers={'authorization':'Bearer operator-only-key'}).status_code == 404


def test_heartbeat_and_cancelled_execution_recovery(env):
    jid=add(env,threshold=None)
    entered=None
    async def scenario():
        entered=asyncio.Event()
        async def handle(request):
            entered.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w=worker(env,client)
            claim=w.claim(jid)
            task=asyncio.create_task(w.execute_claim(claim))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert read(env,jid)['status'] == 'running'
            env[2].advance(121)
            worker(env).recover()
            assert read(env,jid)['status'] == 'unknown'
    asyncio.run(scenario())


def test_interactive_capacity_rejects_without_upstream_call(env):
    settings,_,clock,_=env
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def handle(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200,json=completion())
        app=create_app(settings.model_copy(update={'interactive_concurrency':1}),httpx.MockTransport(handle),clock)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                pending=asyncio.create_task(client.post('/v1/chat/completions',json=body()['request'],headers=HEADERS))
                await entered.wait()
                limited=await client.post('/v1/chat/completions',json=body()['request'],headers=HEADERS)
                assert limited.status_code == 429 and limited.headers['retry-after'] == '1'
                release.set()
                assert (await pending).status_code == 200 and len(calls) == 1
    asyncio.run(scenario())


def test_http_date_retry_guidance_and_cancel_pending_retry(env):
    from email.utils import format_datetime
    jid=add(env,threshold=None)
    date=format_datetime(NOW+timedelta(seconds=30),usegmt=True)
    asyncio.run(run_cycle(env,lambda r:httpx.Response(429,headers={'retry-after':date})))
    with env[1].connect() as conn:
        next_attempt=conn.execute(text('SELECT next_attempt_at FROM jobs WHERE id=:id'),dict(id=jid)).scalar()
    assert datetime.fromisoformat(next_attempt) == NOW+timedelta(seconds=30)
    assert jobs.cancel(env[1],'local',jid,NOW)['status'] == 'cancelled'
    env[2].advance(30)
    asyncio.run(run_cycle(env,lambda r:pytest.fail('Cancelled retry must not dispatch')))


def test_queue_full_response_has_retry_guidance(env):
    settings,engine,clock,_=env
    with TestClient(create_app(settings.model_copy(update={'pending_job_limit':1}),clock=clock)) as client:
        assert client.post('/api/jobs',json=body(),headers=HEADERS).status_code == 202
        response=client.post('/api/jobs',json=body(),headers=HEADERS|{'idempotency-key':'second'})
        assert response.status_code == 429 and response.headers['retry-after'] == '10'
        assert client.post('/api/jobs',json=body(),headers=HEADERS).status_code == 202


def test_runtime_config_and_owner_key_uniqueness():
    from pydantic import ValidationError
    for patch in [{'client_keys':{'local':'other-key'}},{'client_keys':{'other':KEY}},
                  {'operator_key':KEY},{'lease_seconds':60},{'pending_job_limit':0}]:
        with pytest.raises(ValidationError):
            Settings(gateway_key=KEY,**patch)


def test_admission_deadlines_are_separate_from_persisted_policy(env):
    from greenroute.contracts import JobPolicy
    past=JobPolicy.model_validate(body(deadline=-1)['policy'])
    with pytest.raises(ValueError):
        past.ensure_submittable(NOW)
    assert past.deadline_at < NOW  # Still parseable for recovery and existing-key lookup.


def test_sync_orphan_recovery_and_probe_configuration_error(env):
    from greenroute.jobs import stamp
    _,engine,clock,_=env
    old=stamp(NOW-timedelta(seconds=181))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO decisions (request_id,created_at,data) VALUES ('orphan',:at,'{}')"),dict(at=old))
        conn.execute(text("INSERT INTO attempts (id,request_id,endpoint_id,started_at,outcome,uncertain) "
            "VALUES ('orphan-attempt','orphan','us-east-1',:at,'running',0)"),dict(at=old))
        conn.execute(text("UPDATE endpoints SET healthy=0,disabled_reason='circuit_open',probe_after=:at WHERE id='eu-central-1'"),dict(at=old))
    worker(env).recover()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT outcome FROM attempts WHERE id='orphan-attempt'")).scalar() == 'unknown'
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(401))) as client:
            await worker(env,client).probe_due()
    asyncio.run(scenario())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT disabled_reason FROM endpoints WHERE id='eu-central-1'")).scalar() == 'configuration_error'


def test_expired_result_is_hidden_before_cleanup_and_success_is_immutable(env):
    jid=add(env,threshold=None)
    w=worker(env)
    claim=w.claim(jid)
    result=AdapterResult(completion=completion())
    assert w.finish(claim,result=result)
    assert not w.finish(claim,result=AdapterResult(completion={'changed':True}))
    env[2].advance(86400)
    assert read(env,jid)['result'] is None and read(env,jid)['result_expired']


def test_same_key_is_scoped_by_owner(env):
    submission=JobSubmission.model_validate(body())
    first=jobs.submit(env[1],'local','shared',submission,NOW,1000)[0]
    other=jobs.submit(env[1],'other','shared',submission,NOW,1000)[0]
    assert first != other
    with pytest.raises(jobs.JobError) as error:
        jobs.get_job(env[1],'other',first,NOW)
    assert error.value.status == 404
