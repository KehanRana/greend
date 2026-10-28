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
