"""Loopback-only test provider with authenticated failure controls; never used by normal setup."""
import asyncio
import os
import secrets
from collections import Counter
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from greenroute.mock import app as normal_mock

app=FastAPI()
state={'mode':'normal','delay_seconds':0}
received=Counter()


def authorized(request):
    return secrets.compare_digest(request.headers.get('authorization','').encode(),
        f"Bearer {os.environ['GREENROUTE_VERIFY_CONTROL_KEY']}".encode())


@app.api_route('/_verification/control',methods=['GET','POST'])
async def control(request:Request):
    if not authorized(request):return JSONResponse({'error':'unauthorized'},status_code=401)
    if request.method=='POST':
        payload=await request.json()
        if payload.get('mode') not in {'normal','sleep','rate_limit','server_error','auth_error'}:
            return JSONResponse({'error':'invalid mode'},status_code=422)
        state.update(mode=payload['mode'],delay_seconds=float(payload.get('delay_seconds',0)))
    return dict(state,received=dict(received),total_received=sum(received.values()))


@app.middleware('http')
async def inject(request,call_next):
    if request.url.path.endswith('/v1/chat/completions'):
        supplied=request.headers.get('authorization','').encode()
        expected=f"Bearer {os.environ['GREENROUTE_UPSTREAM_KEY']}".encode()
        if not secrets.compare_digest(supplied,expected):return JSONResponse({'error':'unauthorized'},status_code=401)
        received[request.headers.get('x-greenroute-request-id','direct')]+=1
        mode=state['mode']
        if mode=='sleep':await asyncio.sleep(state['delay_seconds'])
        if mode=='rate_limit':return JSONResponse({'error':'simulated rate limit'},status_code=429,headers={'retry-after':'1'})
        if mode=='server_error':return JSONResponse({'error':'simulated provider error'},status_code=503)
        if mode=='auth_error':return JSONResponse({'error':'simulated provider credentials rejected'},status_code=401)
    return await call_next(request)


app.mount('/',normal_mock)
