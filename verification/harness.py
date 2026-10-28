"""Own only temporary databases, generated credentials and explicitly spawned processes."""
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def host_info():
    cpu=platform.processor() or platform.machine()
    if sys.platform=='darwin':
        result=subprocess.run(['sysctl','-n','machdep.cpu.brand_string'],capture_output=True,text=True)
        if result.returncode==0:cpu=result.stdout.strip()
    return dict(os=platform.platform(),architecture=platform.machine(),cpu=cpu,logical_cpus=os.cpu_count(),
                python=platform.python_version())


def job_body(threshold=None,seconds=600):
    return {'request':{'model':'demo-model','messages':[{'role':'user','content':'Synthetic verification document. Summarize flexible inference scheduling.'}]},
            'policy':{'allowed_regions':['us-east-1','eu-central-1','ap-southeast-2'],
                'max_carbon_gco2e_kwh':threshold,'deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),
                'deadline_policy':'expire'}}


class Stack:
    def __init__(self,*,faults=False,delay=0.1):
        self.temporary=tempfile.TemporaryDirectory(prefix='greenroute-verification-')
        self.directory=Path(self.temporary.name)
        self.database=self.directory/'verification.db'
        self.grid_fail_file=self.directory/'grid-failed'
        self.api_port,self.upstream_port=available_port(),available_port()
        while self.upstream_port==self.api_port:self.upstream_port=available_port()
        self.base=f'http://127.0.0.1:{self.api_port}'
        self.upstream=f'http://127.0.0.1:{self.upstream_port}'
        self.key,self.operator_key,self.upstream_key,self.control_key=[secrets.token_urlsafe(24) for _ in range(4)]
        self.faults=faults
        self.env={k:v for k,v in os.environ.items() if not k.startswith('GREENROUTE_')}
        self.env.update(PYTHONPATH=str(ROOT/'backend')+os.pathsep+str(ROOT),GREENROUTE_MODE='demo',
            GREENROUTE_GATEWAY_KEY=self.key,GREENROUTE_OPERATOR_KEY=self.operator_key,
            GREENROUTE_UPSTREAM_KEY=self.upstream_key,GREENROUTE_DATABASE=str(self.database),
            GREENROUTE_UPSTREAM_URL=self.upstream,GREENROUTE_MOCK_DELAY_SECONDS=str(delay),
            GREENROUTE_VERIFY_CONTROL_KEY=self.control_key,GREENROUTE_VERIFY_GRID_FAIL_FILE=str(self.grid_fail_file))
        if faults:
            self.env.update(GREENROUTE_TIMEOUT_SECONDS='1',GREENROUTE_LEASE_SECONDS='2',
                GREENROUTE_WORKER_POLL_SECONDS='0.1',GREENROUTE_RETRY_BASE_SECONDS='0.1',
                GREENROUTE_GRID_REFRESH_SECONDS='1',GREENROUTE_GRID_MAX_AGE_SECONDS='1.5')
        self.processes=[];self.log_handles=[];self.worker=None
        self.client=httpx.Client(base_url=self.base,headers={'authorization':f'Bearer {self.key}'},timeout=15,trust_env=False)

    def spawn(self,args,name):
        log=(self.directory/f'{name}-{len(self.processes)}.log').open('w')
        self.log_handles.append(log)
        p=subprocess.Popen([sys.executable,*args],env=self.env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        self.processes.append(p);return p

    def wait(self,path,p,base=None,seconds=10):
        stop=time.monotonic()+seconds
        while time.monotonic()<stop:
            if p.poll() is not None:raise RuntimeError('Verification service exited before readiness')
            try:
                if httpx.get((base or self.base)+path,timeout=.3,trust_env=False).status_code==200:return
            except httpx.RequestError:pass
            time.sleep(.05)
        raise TimeoutError('Verification service readiness timed out')

    def start_upstream(self):
        target='verification.fault_upstream:app' if self.faults else 'greenroute.mock:app'
        self.provider=self.spawn(['-m','uvicorn',target,'--host','127.0.0.1','--port',str(self.upstream_port),'--log-level','error'],'upstream')
        self.wait('/openapi.json',self.provider,self.upstream)

    def start_gateway(self):
        target='verification.fault_gateway:create_app' if self.faults else 'greenroute.app:create_app'
        self.gateway=self.spawn(['-m','uvicorn',target,'--factory','--host','127.0.0.1','--port',str(self.api_port),'--log-level','error'],'gateway')
        self.wait('/ready',self.gateway)

    def start_worker(self):
        self.worker=self.spawn(['-m','greenroute.worker'],'worker');return self.worker

    def __enter__(self):
        try:
            subprocess.run([sys.executable,'-m','alembic','upgrade','head'],env=self.env,cwd=ROOT,
                           check=True,capture_output=True,timeout=30)
            self.start_upstream();self.start_gateway();return self
        except BaseException:
            self.__exit__(None,None,None);raise

    @staticmethod
    def stop(p,hard=False):
        if p and p.poll() is None:
            p.kill() if hard else p.terminate()
            try:p.wait(timeout=3)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)

    def __exit__(self,*_):
        for p in reversed(self.processes):self.stop(p)
        self.client.close()
        for log in self.log_handles:log.close()
        self.temporary.cleanup()

    def scenario(self,name):
        r=self.client.post('/api/demo/scenario',headers={'authorization':f'Bearer {self.operator_key}'},json={'scenario':name})
        r.raise_for_status()
        if not r.json()['refreshed']:raise RuntimeError('Scenario failed to refresh')
        return r.json()

    def submit(self,body=None,key=None):
        r=self.client.post('/api/jobs',headers={'idempotency-key':key or secrets.token_hex(12)},json=body or job_body())
        r.raise_for_status();return r.json()['id']

    def job(self,jid):
        r=self.client.get(f'/api/jobs/{jid}');r.raise_for_status();return r.json()

    def until(self,predicate,seconds=10):
        stop=time.monotonic()+seconds
        while time.monotonic()<stop:
            value=predicate()
            if value:return value
            time.sleep(.05)
        raise TimeoutError('Verification condition timed out')

    def state(self,jid,state,seconds=10):
        return self.until(lambda:(row if (row:=self.job(jid))['status']==state else None),seconds)

    def control(self,mode=None,delay=0):
        kwargs={'headers':{'authorization':f'Bearer {self.control_key}'}}
        r=httpx.post(self.upstream+'/_verification/control',json={'mode':mode,'delay_seconds':delay},timeout=2,trust_env=False,**kwargs) if mode else httpx.get(self.upstream+'/_verification/control',timeout=2,trust_env=False,**kwargs)
        r.raise_for_status();return r.json()


def write_report(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,indent=2)+'\n');temporary.replace(path)
