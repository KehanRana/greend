"""Replay a synthetic batch with stable keys; optionally demonstrate wait -> clean release."""
import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4

import httpx


def manifest(path,count):
    if path.exists():
        data=json.loads(path.read_text())
        if datetime.fromisoformat(data['created_at'])+timedelta(hours=24)<=datetime.now(timezone.utc):
            raise SystemExit('Run is older than the 24-hour idempotency window; choose a new run file.')
        return data
    now=datetime.now(timezone.utc)
    run=uuid4().hex
    data={'run_id':run,'created_at':now.isoformat(),'requests':[
        {'key':f'demo-{run}-{i}','body':{'request':{'model':'demo-model','messages':[{'role':'user',
            'content':f'Synthetic summarization document {i+1}: flexible workloads can wait for cleaner regional electricity.'}]},
        'policy':{'allowed_regions':['us-east-1','eu-central-1','ap-southeast-2'],'max_carbon_gco2e_kwh':150,
                  'deadline_at':(now+timedelta(minutes=10)).isoformat(),'deadline_policy':'expire'}}} for i in range(count)]}
    path.parent.mkdir(parents=True,exist_ok=True)
    with open(path,'x',opener=lambda name,flags:os.open(name,flags,0o600)) as f:
        json.dump(data,f)
    return data


async def main(args):
    operator=os.environ.get('GREENROUTE_OPERATOR_KEY')
    if args.release_clean and not operator:
        raise SystemExit('--release-clean requires GREENROUTE_OPERATOR_KEY')
    run=manifest(args.run_file,args.count)
    semaphore=asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url,headers={'authorization':f"Bearer {os.environ['GREENROUTE_GATEWAY_KEY']}"},
                                 timeout=60,trust_env=False) as client:
        async def scenario(name):
            response=await client.post('/api/demo/scenario',headers={'authorization':f'Bearer {operator}'},json={'scenario':name})
            response.raise_for_status()
            if not response.json()['refreshed']:raise RuntimeError('Scenario stored but grid refresh failed')
        if args.release_clean:await scenario('high')
        async def submit(item):
            async with semaphore:
                for attempt in range(3):
                    try:
                        response=await client.post('/api/jobs',headers={'idempotency-key':item['key']},json=item['body'])
                        if response.status_code in {429,502,503,504} and attempt<2:
                            await asyncio.sleep(1+attempt);continue
                        response.raise_for_status()
                        return response.json()['id']
                    except httpx.RequestError:
                        if attempt==2:raise
                        await asyncio.sleep(1+attempt)
        results=await asyncio.gather(*(submit(item) for item in run['requests']),return_exceptions=True)
        ids=[r for r in results if isinstance(r,str)]
        if len(ids)!=len(results):
            raise SystemExit(f'{len(ids)}/{len(results)} submissions confirmed. Rerun the same file to retry stable keys: {args.run_file}')
        print(f'Confirmed {len(ids)} jobs. Run file: {args.run_file}',flush=True)
        async def status(jid):
            async with semaphore:
                response=await client.get(f'/api/jobs/{jid}');response.raise_for_status();return response.json()['status']
        released=False
        last=None
        for _ in range(600):
            states=await asyncio.gather(*(status(jid) for jid in ids))
            counts=dict(Counter(states))
            if counts!=last:print(json.dumps({'this_run':counts}),flush=True);last=counts
            terminal={'succeeded','failed','expired','cancelled','unknown'}
            if args.release_clean and not released and all(s=='waiting_for_carbon' or s in terminal for s in states):
                await scenario('clean');released=True;print('Applied clean Frankfurt scenario.',flush=True)
            if all(s in terminal for s in states):
                response=await client.get('/api/metrics');response.raise_for_status()
                print(json.dumps({'run_id':run['run_id'],'this_run':counts,'workspace_metrics':response.json()},indent=2))
                if counts.get('succeeded',0)!=len(ids):raise SystemExit('Some jobs did not succeed; inspect their owner-authorized job records.')
                return
            await asyncio.sleep(1)
        raise SystemExit('Polling stopped; accepted jobs remain durable. Resume with the same run file.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    parser.add_argument('--count',type=int,default=500)
    parser.add_argument('--concurrency',type=int,default=10)
    parser.add_argument('--run-file',type=Path,required=True,help='Synthetic bodies/keys only; never contains credentials')
    parser.add_argument('--release-clean',action='store_true',help='Apply high signals, wait for admission, then release clean signals across the demo workspace')
    args=parser.parse_args()
    if not 1<=args.count<=1000 or not 1<=args.concurrency<=50:parser.error('Count must be 1–1000; concurrency 1–50')
    asyncio.run(main(args))
