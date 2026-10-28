"""Exercise the documented two-process setup and a real gateway restart."""
import os
import socket
import subprocess
import sys
import time

import httpx
from openai import OpenAI


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(url, process):
    for _ in range(100):
        assert process.poll() is None, "Server exited before readiness"
        try:
            if httpx.get(url, timeout=.2, trust_env=False).status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(.05)
    raise AssertionError("Server readiness timed out")


def test_real_process_restart(tmp_path):
    gateway_port, upstream_port = port(), port()
    env = os.environ | {"PYTHONPATH":"backend", "GREENROUTE_GATEWAY_KEY":"smoke-local-key",
        "GREENROUTE_DATABASE":str(tmp_path/'smoke.db'), "GREENROUTE_MODE":"demo",
        "GREENROUTE_UPSTREAM_URL":f"http://127.0.0.1:{upstream_port}",
        "GREENROUTE_UPSTREAM_KEY":"separate-upstream-key"}
    subprocess.run([sys.executable,"-m","alembic","upgrade","head"],env=env,check=True,capture_output=True)
    processes = []
    def start(target, p, factory=False):
        args = [sys.executable,"-m","uvicorn",target,"--host","127.0.0.1","--port",str(p),"--log-level","error"]
        if factory:
            args.append("--factory")
        process = subprocess.Popen(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        processes.append(process)
        return process
    try:
        upstream = start("greenroute.mock:app",upstream_port)
        # Mock has no health endpoint; schema availability confirms startup.
        wait(f"http://127.0.0.1:{upstream_port}/openapi.json",upstream)
        gateway = start("greenroute.app:create_app",gateway_port,True)
        base = f"http://127.0.0.1:{gateway_port}"
        wait(base+"/ready",gateway)
        with OpenAI(api_key=env['GREENROUTE_GATEWAY_KEY'],base_url=base+"/v1",max_retries=0) as sdk:
            response = sdk.chat.completions.with_raw_response.create(model="demo-model",
                messages=[{"role":"user","content":"private smoke input"}])
            assert response.parse().usage.total_tokens == 56
            request_id = response.headers['x-greenroute-request-id']
            assert response.headers['x-greenroute-region'] == 'us-east-1'
        gateway.terminate()
        gateway.wait(timeout=5)
        restarted = start("greenroute.app:create_app",gateway_port,True)
        wait(base+"/ready",restarted)
        activity = httpx.get(base+"/api/activity",headers={"authorization":"Bearer smoke-local-key"},trust_env=False)
        assert activity.json()['items'][0]['request_id'] == request_id
        assert activity.json()['items'][0]['outcome'] == 'succeeded'
        assert 'private smoke input' not in activity.text
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_real_job_worker_and_pending_restart(tmp_path):
    from datetime import datetime,timedelta,timezone
    gateway_port,upstream_port=port(),port()
    env=os.environ | {'PYTHONPATH':'backend','GREENROUTE_GATEWAY_KEY':'process-jobs-key',
        'GREENROUTE_DATABASE':str(tmp_path/'jobs.db'),'GREENROUTE_MODE':'demo',
        'GREENROUTE_UPSTREAM_URL':f'http://127.0.0.1:{upstream_port}',
        'GREENROUTE_UPSTREAM_KEY':'process-upstream-key','GREENROUTE_WORKER_POLL_SECONDS':'0.1',
        'GREENROUTE_OPERATOR_KEY':'process-operator-key'}
    subprocess.run([sys.executable,'-m','alembic','upgrade','head'],env=env,check=True,capture_output=True)
    processes=[]
    def start(args):
        process=subprocess.Popen([sys.executable,*args],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        processes.append(process)
        return process
    def gateway():
        process=start(['-m','uvicorn','greenroute.app:create_app','--factory','--host','127.0.0.1','--port',str(gateway_port)])
        wait(base+'/ready',process)
        return process
    base=f'http://127.0.0.1:{gateway_port}'
    headers={'authorization':'Bearer process-jobs-key','idempotency-key':'process-job'}
    try:
        upstream=start(['-m','uvicorn','greenroute.mock:app','--host','127.0.0.1','--port',str(upstream_port)])
        wait(f'http://127.0.0.1:{upstream_port}/openapi.json',upstream)
        api=gateway()
        body={'request':{'model':'demo-model','messages':[{'role':'user','content':'private process job'}]},
              'policy':{'allowed_regions':['eu-central-1'],'max_carbon_gco2e_kwh':None,
                        'deadline_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
                        'deadline_policy':'expire'}}
        response=httpx.post(base+'/api/jobs',headers=headers,json=body,trust_env=False)
        assert response.status_code == 202
        jid=response.json()['id']
        api.terminate()
        api.wait(timeout=5)
        gateway()
        assert httpx.get(base+f'/api/jobs/{jid}',headers=headers,trust_env=False).json()['status'] == 'queued'
        runner=start(['-m','greenroute.worker'])
        for _ in range(100):
            assert runner.poll() is None,'Worker unexpectedly exited'
            detail=httpx.get(base+f'/api/jobs/{jid}',headers=headers,trust_env=False).json()
            if detail['status'] == 'succeeded':
                break
            time.sleep(.05)
        else:
            raise AssertionError('Job did not complete through worker process')
        assert detail['attempt_count'] == 1 and detail['decision']['selected_endpoint'] == 'eu-central-1'
        assert detail['result']['usage']['total_tokens'] == 56
        retry=httpx.post(base+'/api/jobs',headers=headers,json=body,trust_env=False)
        assert retry.json()['id'] == jid
        activity=httpx.get(base+'/api/activity',headers=headers,trust_env=False)
        assert 'private process job' not in activity.text
        example=subprocess.run([sys.executable,'examples/batch.py','--base-url',base],env=env,capture_output=True,text=True,timeout=10)
        assert example.returncode == 0, example.stderr
        assert 'Status: succeeded' in example.stdout
        command=[sys.executable,'examples/demo_batch.py','--base-url',base,'--count','8','--release-clean',
                 '--run-file',str(tmp_path/'replay.json')]
        replay=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
        assert replay.returncode == 0,replay.stderr
        assert '"succeeded": 8' in replay.stdout
        before=httpx.get(base+'/api/metrics',headers=headers,trust_env=False).json()['succeeded_executions']
        resumed=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
        assert resumed.returncode == 0,resumed.stderr
        assert httpx.get(base+'/api/metrics',headers=headers,trust_env=False).json()['succeeded_executions'] == before
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
