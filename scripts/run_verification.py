"""Run isolated GreenRoute smoke, performance and physical-failure verification."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime,timezone
from importlib.metadata import version
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'backend')]
import httpx
from openai import OpenAI
from sqlalchemy import text
from greenroute.store import make_engine
from verification.harness import Stack,host_info,job_body,write_report


def distribution(samples):
    values=sorted(samples)
    if not values:return None
    return dict(n=len(values),min_ms=round(values[0],3),median_ms=round(statistics.median(values),3),
        mean_ms=round(statistics.mean(values),3),p95_ms=round(values[math.ceil(.95*len(values))-1],3),
        p99_ms=round(values[math.ceil(.99*len(values))-1],3),max_ms=round(values[-1],3))


def smoke(stack):
    stack.scenario('clean')
    with OpenAI(api_key=stack.key,base_url=stack.base+'/v1',max_retries=0) as sdk:
        response=sdk.chat.completions.with_raw_response.create(model='demo-model',
            messages=[{'role':'user','content':'Synthetic SDK verification input.'}])
        assert response.parse().usage.total_tokens==56
        assert response.headers['x-greenroute-mode']=='demo'
    jid=stack.submit()
    assert stack.job(jid)['status']=='queued'
    stack.stop(stack.gateway);stack.start_gateway()
    assert stack.job(jid)['status']=='queued'
    stack.start_worker()
    result=stack.state(jid,'succeeded',15)
    assert result['attempt_count']==1 and result['decision']['selected_endpoint']=='eu-central-1'
    activity=stack.client.get('/api/activity');activity.raise_for_status()
    assert 'Synthetic SDK verification input.' not in activity.text
    dashboard=stack.client.get('/dashboard/');dashboard.raise_for_status()
    assets=re.findall(r'(?:src|href)="(/dashboard/assets/[^\"]+)"',dashboard.text)
    assert assets,'Dashboard must be built before verification'
    for asset in assets:stack.client.get(asset).raise_for_status()
    for license in ('font-license.txt','map-license.txt'):stack.client.get('/dashboard/'+license).raise_for_status()
    return dict(status='passed',official_sdk=True,pending_survives_api_restart=True,worker_completion=True,
                dashboard_assets_checked=len(assets),bundled_licenses=True,metadata_content_redacted=True)


async def latency(stack,count,concurrency):
    payload=job_body()['request']
    async with httpx.AsyncClient(timeout=15,trust_env=False,
        limits=httpx.Limits(max_connections=concurrency*2,max_keepalive_connections=concurrency*2)) as client:
        async def timed(route):
            start=time.perf_counter()
            url=stack.base+'/v1/chat/completions' if route=='gateway' else stack.upstream+'/us-east-1/v1/chat/completions'
            key=stack.key if route=='gateway' else stack.upstream_key
            response=await client.post(url,headers={'authorization':f'Bearer {key}'},json=payload)
            elapsed=(time.perf_counter()-start)*1000
            response.raise_for_status()
            assert response.json()['usage']['total_tokens']==56
            return elapsed
        async def measure(n):
            samples=[]
            async def lane(lane):
                for index in range(lane,n,concurrency):
                    if index%2:
                        gateway=await timed('gateway');direct=await timed('direct')
                    else:
                        direct=await timed('direct');gateway=await timed('gateway')
                    samples.append(dict(index=index,direct_ms=direct,gateway_ms=gateway,overhead_ms=gateway-direct))
            await asyncio.gather(*(lane(i) for i in range(concurrency)))
            return sorted(samples,key=lambda row:row['index'])
        await measure(30)
        started=time.perf_counter();samples=await measure(count);elapsed=time.perf_counter()-started
    overhead=distribution([s['overhead_ms'] for s in samples])
    standard=count==1000 and concurrency==10
    return dict(status='passed' if standard and overhead['p95_ms']<=100 else ('failed' if standard else 'nonstandard'),
        method='nearest-rank p95 of per-pair (gateway RTT - direct RTT); pair order alternates; one outstanding request per lane',
        exclusions='30 warm-up pairs excluded. Difference removes matched upstream RTT approximately; network/scheduler jitter remains. Negative differences are retained.',
        samples=count,concurrency=concurrency,nominal_mock_delay_ms=100,warmup_pairs=30,errors=0,elapsed_seconds=round(elapsed,3),
        direct=distribution([s['direct_ms'] for s in samples]),gateway=distribution([s['gateway_ms'] for s in samples]),
        paired_overhead=overhead,target_p95_overhead_ms=100,
        raw_pairs_ms=[{k:round(v,3) if isinstance(v,float) else v for k,v in sample.items()} for sample in samples])


async def batch(stack,count,concurrency):
    stack.scenario('high')
    body=job_body(threshold=150)
    run='benchmark-'+hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    semaphore=asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=stack.base,headers={'authorization':f'Bearer {stack.key}'},timeout=30,trust_env=False) as client:
        async def submit(index):
            async with semaphore:
                response=await client.post('/api/jobs',headers={'idempotency-key':f'{run}-{index}'},json=body)
                response.raise_for_status();assert response.status_code==202
                return response.json()['id']
        started=time.perf_counter();ids=set(await asyncio.gather(*(submit(i) for i in range(count))))
        assert len(ids)==count
        admission=time.perf_counter()-started
        async def states():
            requests=[client.get(f'/api/jobs?limit=100&offset={offset}') for offset in range(0,count+100,100)]
            responses=await asyncio.gather(*requests)
            result={}
            for response in responses:
                response.raise_for_status()
                result.update({r['id']:r['state'] for r in response.json()['items'] if r['id'] in ids})
            assert len(result)==count,'An acknowledged job disappeared'
            return Counter(result.values())
        wait_started=time.perf_counter()
        while await states()!=Counter({'waiting_for_carbon':count}):
            assert time.perf_counter()-wait_started<60,'Not all jobs entered carbon waiting'
            await asyncio.sleep(.25)
        before=(await client.get('/api/metrics')).json()
        release=time.perf_counter()
        changed=await client.post('/api/demo/scenario',headers={'authorization':f'Bearer {stack.operator_key}'},json={'scenario':'clean'})
        changed.raise_for_status();assert changed.json()['refreshed']
        release_ack=time.perf_counter()-release
        while (final:=await states())!=Counter({'succeeded':count}):
            assert not any(final[s] for s in ('failed','expired','unknown','cancelled')),dict(final)
            assert time.perf_counter()-release<300,'Batch drain exceeded five minutes'
            await asyncio.sleep(.25)
        drain=time.perf_counter()-release
        after=(await client.get('/api/metrics')).json()
    delta=round(after['estimated_difference_g']-before['estimated_difference_g'],6)
    assert after['succeeded_executions']-before['succeeded_executions']==count
    assert after['compared_executions']-before['compared_executions']==count
    assert delta==round(count*56/1000*.001*(400-80),6)
    engine=make_engine(stack.database)
    try:
        with engine.connect() as conn:
            attempts=conn.execute(text('SELECT COUNT(*) FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE idempotency_key LIKE :prefix)'),dict(prefix=run+'%')).scalar()
        assert attempts==count,'Unexpected replay or extra attempt'
    finally:engine.dispose()
    standard=count==500 and concurrency==10
    return dict(status='passed' if standard and drain<=300 else ('failed' if standard else 'nonstandard'),
        acknowledged_jobs=count,unique_jobs=len(ids),completed_jobs=count,attempts=attempts,
        confirmed_carbon_wait=True,nominal_mock_delay_ms=100,worker_concurrency=10,worker_poll_seconds=10,
        admission_seconds=round(admission,3),release_ack_seconds=round(release_ack,3),
        drain_after_release_seconds=round(drain,3),target_drain_seconds=300,poll_detection_interval_seconds=.25,
        observed_estimated_difference_g=delta,note='Synthetic spatial estimate only, not measured avoided emissions')


def failures():
    cases=[]
    with Stack(faults=True,delay=0) as stack:
        engine=make_engine(stack.database)
        try:
            stack.control('sleep',30)
            jid=stack.submit();stack.start_worker()
            running=stack.state(jid,'running')
            stack.until(lambda:stack.control()['total_received']>=1)
            rid=running['attempts'][0]['request_id']
            stack.stop(stack.worker,hard=True)
            assert stack.job(jid)['status']=='running'
            stack.start_worker()
            unknown=stack.state(jid,'unknown',8)
            assert unknown['attempt_count']==1 and unknown['result'] is None
            assert stack.control()['received'][rid]==1
            cases.append(dict(name='worker_sigkill_after_provider_accept',status='passed',attempts=1,provider_receipts=1,
                              recovered_state='unknown',automatic_replay=False))
            stack.stop(stack.provider,hard=True)
            failed=stack.submit()
            row=stack.state(failed,'failed',8)
            assert row['attempt_count']==3
            stack.start_upstream()
            stack.client.post('/api/operator/endpoints/eu-central-1/enable',headers={'authorization':f'Bearer {stack.operator_key}'}).raise_for_status()
            recovered=stack.submit();stack.state(recovered,'succeeded')
            cases.append(dict(name='provider_process_outage_and_operator_recovery',status='passed',bounded_attempts=3,recovered=True))
            stack.control('rate_limit')
            limited=stack.submit();stack.state(limited,'retry_pending')
            stack.control('normal')
            row=stack.state(limited,'succeeded')
            assert row['attempt_count']==2
            times=[datetime.fromisoformat(a['started_at']) for a in row['attempts']]
            gap=(times[1]-times[0]).total_seconds()
            assert gap>=1,'Retry-After was not honored'
            cases.append(dict(name='rate_limit_recovery',status='passed',attempts=2,retry_start_gap_seconds=round(gap,3)))
            with engine.begin() as conn:
                before=conn.execute(text('SELECT COUNT(*) FROM jobs')).scalar()
                conn.exec_driver_sql("CREATE TRIGGER verification_fail BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'simulated storage write failure'); END")
            body=job_body();key='storage-failure-retry'
            response=stack.client.post('/api/jobs',headers={'idempotency-key':key},json=body)
            assert response.status_code==503 and 'simulated storage' not in response.text
            with engine.begin() as conn:
                assert conn.execute(text('SELECT COUNT(*) FROM jobs')).scalar()==before
                conn.exec_driver_sql('DROP TRIGGER verification_fail')
            committed=stack.submit(body,key);stack.state(committed,'succeeded')
            cases.append(dict(name='admission_write_failure',status='passed',failure_status=503,false_acknowledgments=0,same_key_retry=True,
                              injection='SQLite abort trigger, not actual disk exhaustion'))
            stack.scenario('high')
            stack.grid_fail_file.touch()
            initial=stack.client.get('/api/regions').json()['items']
            observed={r['region']:r['observed_at'] for r in initial}
            def degraded():
                data=stack.client.get('/api/regions').json()
                return data if data['poller']['status']=='degraded' and all(r['signal_status']=='stale' for r in data['items']) else None
            stale=stack.until(degraded,8)
            assert {r['region']:r['observed_at'] for r in stale['items']}==observed
            before=stack.control()['total_received']
            blocked=stack.submit(job_body(threshold=150,seconds=2))
            stack.state(blocked,'waiting_for_carbon');stack.state(blocked,'expired',8)
            assert stack.control()['total_received']==before
            interactive=stack.client.post('/v1/chat/completions',json=job_body()['request'])
            assert interactive.status_code==200
            stack.grid_fail_file.unlink();stack.scenario('clean')
            stack.state(stack.submit(),'succeeded')
            cases.append(dict(name='grid_outage_stale_cache_and_recovery',status='passed',source_timestamps_preserved=True,
                              carbon_job='expired_without_dispatch',interactive_status=200,recovered=True,
                              test_freshness_seconds=1.5,test_source='per-fetch synthetic observations'))
        finally:engine.dispose()
    return cases


def main(args):
    report=dict(schema_version=1,started_at=datetime.now(timezone.utc).isoformat(),mode='isolated-demo',
        profile='smoke' if args.smoke_only else 'standard',host=host_info(),
        dependencies={name:version(name) for name in ('fastapi','uvicorn','httpx','sqlalchemy','openai')},
        lock_sha256=hashlib.sha256((ROOT/'requirements.lock').read_bytes()).hexdigest())
    phase='startup';started=time.perf_counter()
    try:
        print('Starting isolated mock/gateway/worker smoke check',flush=True)
        with Stack(delay=.1) as stack:
            phase='smoke';report['smoke']=smoke(stack)
            if not args.smoke_only:
                phase='latency';print('Measuring 1,000 direct/gateway pairs at concurrency 10',flush=True)
                report['latency']=asyncio.run(latency(stack,args.requests,args.concurrency))
                print('Latency p95 overhead:',report['latency']['paired_overhead']['p95_ms'],'ms',flush=True)
                phase='batch';print('Measuring 500-job wait/release/drain with 100 ms mock latency',flush=True)
                report['batch']=asyncio.run(batch(stack,args.jobs,args.concurrency))
                print('Drain:',report['batch']['drain_after_release_seconds'],'seconds',flush=True)
        if not args.smoke_only:
            phase='faults';print('Checking hard crash, provider outage, retries, storage failure and grid outage',flush=True)
            report['faults']=failures()
        report['status']='passed'
        if not args.smoke_only and any(report[k]['status']!='passed' for k in ('latency','batch')):report['status']='failed'
    except Exception as exc:
        report.update(status='failed',failed_phase=phase,error_type=type(exc).__name__,error=str(exc))
    report['elapsed_seconds']=round(time.perf_counter()-started,3)
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    write_report(args.output,report)
    print(f"Verification {report['status']}; report: {args.output}",flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'verification'/'m5-report.json')
    parser.add_argument('--smoke-only',action='store_true')
    parser.add_argument('--requests',type=int,default=1000)
    parser.add_argument('--jobs',type=int,default=500)
    parser.add_argument('--concurrency',type=int,default=10)
    args=parser.parse_args()
    if not 1<=args.jobs<=1000 or args.requests<10 or not 1<=args.concurrency<=50:parser.error('Invalid verification size')
    raise SystemExit(main(args))
