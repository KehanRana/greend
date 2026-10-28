import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from uuid import uuid4
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Request, Query, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text, bindparam
from sqlalchemy.exc import SQLAlchemyError

from greenroute.config import Settings
from greenroute.clock import SystemClock
from greenroute.grid import FixtureGridAdapter, GridPoller, latest_snapshots, signal_status
from greenroute.contracts import ChatRequest, JobSubmission, DemoScenario
from greenroute.estimation import capture_context, persist, public_estimate, metrics
from greenroute.scenarios import DatabaseFixtureGridAdapter, read_scenario, set_scenario
from greenroute import jobs
from greenroute.endpoint_health import record_outcome, enable
from greenroute.policy import interactive_decision
from greenroute.inference import execute, UpstreamFailure
from greenroute.store import make_engine, ready, utcnow


def error(status, message, code):
    return JSONResponse({"error": {"message": message, "type": "gateway_error", "code": code}}, status_code=status)


def create_app(settings=None, transport=None, clock=None, grid_adapter=None):
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database)
    clock = clock or SystemClock()
    poller = GridPoller(engine, grid_adapter or DatabaseFixtureGridAdapter(engine,clock), clock,
                        settings.grid_refresh_seconds, settings.grid_max_age_seconds,
                        settings.grid_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_seconds, connect=5),
                                     transport=transport, trust_env=False) as client:
            app.state.client = client
            await poller.refresh()
            poll_task = asyncio.create_task(poller.run(), name="grid-poller")
            try:
                yield
            finally:
                poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poll_task
        engine.dispose()

    app = FastAPI(title="GreenRoute — simulated MVP", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.grid_poller = poller

    async def authenticated(request: Request):
        from fastapi import HTTPException
        supplied = request.headers.get("authorization", "").encode()
        credentials = {"local":settings.gateway_key} | settings.client_keys
        for owner,key in credentials.items():
            if secrets.compare_digest(supplied,f"Bearer {key.get_secret_value()}".encode()):
                return owner
        raise HTTPException(status_code=401,detail="Invalid gateway credentials")

    async def operator(request: Request):
        from fastapi import HTTPException
        if not settings.operator_key or not secrets.compare_digest(request.headers.get("authorization","").encode(),
                f"Bearer {settings.operator_key.get_secret_value()}".encode()):
            raise HTTPException(status_code=401,detail="Operator credentials required")

    slots = asyncio.Semaphore(settings.interactive_concurrency)

    async def interactive_slot():
        from fastapi import HTTPException
        if slots.locked():
            raise HTTPException(status_code=429,detail="Interactive capacity reached",headers={"retry-after":"1"})
        async with slots:
            yield

    @app.exception_handler(jobs.JobError)
    async def job_error(request,exc):
        response=error(exc.status,exc.message,exc.code)
        if exc.status == 429:
            response.headers["retry-after"]="10"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request,exc):
        return error(503,"Storage unavailable; retry with the same idempotency key","storage_unavailable")

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        status = 400 if any(e["type"] == "json_invalid" for e in exc.errors()) else 422
        return error(status, "Invalid or unsupported fields; see /docs for supported text chat schema", "validation_error")

    from starlette.exceptions import HTTPException

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        response = error(exc.status_code, str(exc.detail), "http_error")
        response.headers.update(exc.headers or {})
        return response

    @app.middleware("http")
    async def body_limit(request, call_next):
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024 * 1024:
                    return error(413, "Request exceeds 1 MiB", "payload_too_large")
            request._body = bytes(body)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": settings.mode}

    @app.get("/ready")
    async def readiness():
        try:
            ready(engine)
        except Exception:
            return error(503, "Storage unavailable or migrations required", "not_ready")
        return {"status": "ready", "mode": settings.mode}

    @app.get("/api/regions", dependencies=[Depends(authenticated)])
    async def regions():
        try:
            snapshots = latest_snapshots(engine)
            with engine.connect() as conn:
                endpoints = conn.execute(text("SELECT * FROM endpoints ORDER BY latency_rank,id")).mappings().all()
        except Exception:
            return error(503, "Regional metadata unavailable; check storage migrations", "regions_unavailable")
        now = clock.now()
        items = []
        for endpoint in endpoints:
            snapshot = snapshots.get(endpoint["grid_zone"])
            status = signal_status(snapshot, now, settings.grid_max_age_seconds)
            items.append(dict(endpoint_id=endpoint["id"], region=endpoint["region"],
                grid_zone=endpoint["grid_zone"], model=endpoint["model"],
                model_version=endpoint["model_version"], healthy=bool(endpoint["healthy"]),
                locality=endpoint["locality"], latency_rank=endpoint["latency_rank"],
                disabled_reason=endpoint["disabled_reason"], consecutive_failures=endpoint["consecutive_failures"],
                signal_status=status, intensity_gco2e_kwh=snapshot.intensity if snapshot else None,
                snapshot_id=snapshot.id if snapshot else None, source=snapshot.source if snapshot else None,
                observed_at=snapshot.observed_at.isoformat() if snapshot else None,
                retrieved_at=snapshot.retrieved_at.isoformat() if snapshot else None,
                age_seconds=(now-snapshot.observed_at).total_seconds() if snapshot else None,
                quality_flags=list(snapshot.quality_flags) if snapshot else []))
        return {"mode": "demo", "evaluated_at": now.isoformat(), "poller": poller.metadata(), "items": items}

    @app.get("/api/metrics")
    async def read_metrics(owner: str = Depends(authenticated), mode: Literal['demo','live'] = 'demo'):
        return metrics(engine,owner,mode,clock.now()) | {'configuration':{
            'baseline_endpoint':settings.baseline_endpoint,'energy_model_version':settings.energy_model_version,
            'kwh_per_1000_tokens':settings.energy_kwh_per_1000_tokens}}

    @app.get("/api/demo/scenario",dependencies=[Depends(authenticated)])
    async def current_scenario():
        if settings.mode != 'demo':
            return error(404,"Demo controls unavailable","not_found")
        return {"mode":"demo",**read_scenario(engine)}

    @app.post("/api/demo/scenario",dependencies=[Depends(operator)])
    async def change_scenario(payload: DemoScenario):
        if settings.mode != 'demo':
            return error(404,"Demo controls unavailable","not_found")
        if not isinstance(poller.adapter,DatabaseFixtureGridAdapter):
            return error(409,"Custom source cannot be changed by demo controls","custom_grid_source")
        set_scenario(engine,payload.scenario,clock.now())
        refreshed=await poller.refresh()
        return {"mode":"demo","refreshed":refreshed,**read_scenario(engine)}

    @app.post("/api/jobs",status_code=202)
    async def submit_job(payload: JobSubmission, owner: str = Depends(authenticated),
                         idempotency_key: str = Header(min_length=1,max_length=128,pattern=r"^[A-Za-z0-9._:-]+$")):
        jid,state=jobs.submit(engine,owner,idempotency_key,payload,clock.now(),settings.pending_job_limit)
        return {"id":jid,"status":state,"status_url":f"/api/jobs/{jid}"}

    @app.get("/api/jobs/{job_id}")
    async def read_job(job_id: str, owner: str = Depends(authenticated)):
        return jobs.get_job(engine,owner,job_id,clock.now())

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, owner: str = Depends(authenticated)):
        return jobs.cancel(engine,owner,job_id,clock.now())

    @app.get("/api/jobs")
    async def list_jobs(owner: str = Depends(authenticated),limit: int = Query(default=20,ge=1,le=100),
                        offset: int = Query(default=0,ge=0),pending: bool = False):
        with engine.connect() as conn:
            rows=conn.execute(text("SELECT j.id,j.state,j.created_at,j.deadline_at,j.attempt_count,json_extract(j.policy,'$.max_carbon_gco2e_kwh') AS carbon_threshold,"
                                   "json_extract(d.data,'$.reason') AS reason FROM jobs j LEFT JOIN decisions d ON d.request_id=j.last_decision_id "
                                   "WHERE j.owner_id=:owner AND (:pending=0 OR j.state IN ('queued','waiting_for_carbon','running','retry_pending')) "
                                   "ORDER BY j.created_at DESC,j.id LIMIT :limit OFFSET :offset"),
                              dict(owner=owner,limit=limit,offset=offset,pending=pending)).mappings().all()
        return {"mode":"demo","items":[dict(r) for r in rows]}

    @app.post("/api/operator/endpoints/{endpoint_id}/enable",dependencies=[Depends(operator)])
    async def enable_endpoint(endpoint_id: str):
        if not enable(engine,endpoint_id):
            return error(404,"Endpoint not found","endpoint_not_found")
        return {"endpoint_id":endpoint_id,"healthy":True,"mode":"demo"}

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticated), Depends(interactive_slot)])
    async def completion(payload: ChatRequest, request: Request):
        for header in request.headers:
            if header.startswith("x-carbon-") and header != "x-carbon-priority":
                return error(422, "Unsupported routing header", "unsupported_policy")
        if (request.headers.get("x-carbon-priority", "high") != "high"
                or "x-task-type" in request.headers or "x-max-carbon" in request.headers):
            return error(422, "Deferred policies require /api/jobs", "unsupported_policy")
        request_id = "req_" + uuid4().hex
        with engine.begin() as conn:
            endpoints = conn.execute(text("SELECT * FROM endpoints ORDER BY latency_rank, id")).mappings().all()
            decision = interactive_decision(request_id, payload.model, endpoints)
            selected = next((e for e in endpoints if e["id"] == decision.selected_endpoint), None)
            if selected:
                decision.estimation_context=capture_context(settings,payload.model,endpoints,decision.eligible,
                                                             latest_snapshots(engine),clock.now())
            conn.execute(text("INSERT INTO decisions (request_id,created_at,data) VALUES (:id,:at,:data)"),
                         dict(id=request_id, at=jobs.stamp(clock.now()), data=decision.model_dump_json()))
            if selected:
                attempt_id = "attempt_" + uuid4().hex
                conn.execute(text("INSERT INTO attempts (id,request_id,endpoint_id,started_at,outcome,uncertain) "
                                  "VALUES (:id,:request,:endpoint,:at,'running',0)"),
                             dict(id=attempt_id, request=request_id, endpoint=selected["id"], at=jobs.stamp(clock.now())))
        if not selected:
            return error(503, "No eligible simulated endpoint for this model", "no_eligible_endpoint")
        failure = None
        outcome, usage, upstream_id, uncertain = "failed", None, None, False
        try:
            adapted = await execute(app.state.client, settings, selected["region"], payload, request_id)
            usage, upstream_id = adapted.usage, adapted.upstream_request_id
            outcome = "succeeded"
            result = JSONResponse(adapted.completion)
        except UpstreamFailure as exc:
            failure = exc
            uncertain = exc.uncertain
            outcome = "unknown" if uncertain else "failed"
            result = error(exc.status, exc.message, exc.code)
            if exc.retry_after and exc.retry_after.isdigit():
                result.headers["retry-after"] = exc.retry_after
        with engine.begin() as conn:
            conn.execute(text("UPDATE attempts SET ended_at=:at,outcome=:outcome,usage=:usage,"
                              "upstream_request_id=:upstream,uncertain=:uncertain WHERE id=:id"),
                         dict(at=jobs.stamp(clock.now()), outcome=outcome, usage=json.dumps(usage) if usage else None,
                              upstream=upstream_id, uncertain=uncertain, id=attempt_id))
            if outcome == 'succeeded':
                persist(conn,attempt_id,decision.model_dump(),usage,clock.now())
        record_outcome(engine,selected["id"],failure,clock.now())
        result.headers["x-greenroute-request-id"] = request_id
        result.headers["x-greenroute-region"] = selected["region"]
        result.headers["x-greenroute-mode"] = "demo"
        return result

    @app.get("/api/activity", dependencies=[Depends(authenticated)])
    async def activity(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT d.request_id,d.created_at,d.data,a.outcome,a.usage,a.uncertain,a.id AS execution_id "
                                     "FROM decisions d LEFT JOIN attempts a ON a.request_id=d.request_id "
                                     "ORDER BY d.created_at DESC,d.request_id LIMIT :limit OFFSET :offset"),
                                dict(limit=limit, offset=offset)).mappings().all()
            ids=[r['execution_id'] for r in rows if r['execution_id']]
            estimates={r['execution_id']:r for r in conn.execute(text('SELECT * FROM emissions_estimates WHERE execution_id IN :ids')
                .bindparams(bindparam('ids',expanding=True)),dict(ids=ids)).mappings()} if ids else {}
        return {"mode": "demo", "items": [dict(request_id=r["request_id"], created_at=r["created_at"],
                    decision=json.loads(r["data"]), outcome=r["outcome"],estimate=public_estimate(estimates.get(r["execution_id"])),
                    usage=json.loads(r["usage"]) if r["usage"] else None, uncertain=bool(r["uncertain"])) for r in rows]}

    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import RedirectResponse
    frontend=Path(__file__).resolve().parents[2]/'frontend'/'dist'
    if frontend.is_dir():
        app.mount('/dashboard',StaticFiles(directory=frontend,html=True),name='dashboard')
        @app.get('/',include_in_schema=False)
        async def dashboard():
            return RedirectResponse('/dashboard/')
    return app
