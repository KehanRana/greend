import json
import secrets
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from greenroute.config import Settings
from greenroute.contracts import ChatRequest
from greenroute.policy import interactive_decision
from greenroute.inference import execute, UpstreamFailure
from greenroute.store import make_engine, ready, utcnow


def error(status, message, code):
    return JSONResponse({"error": {"message": message, "type": "gateway_error", "code": code}}, status_code=status)


def create_app(settings=None, transport=None):
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_seconds, connect=5),
                                     transport=transport, trust_env=False) as client:
            app.state.client = client
            yield
        engine.dispose()

    app = FastAPI(title="GreenRoute — simulated MVP", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    async def authenticated(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""),
                                      f"Bearer {settings.gateway_key.get_secret_value()}"):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid gateway credentials")

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        status = 400 if any(e["type"] == "json_invalid" for e in exc.errors()) else 422
        return error(status, "Invalid or unsupported fields; see /docs for supported text chat schema", "validation_error")

    from starlette.exceptions import HTTPException

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(exc.status_code, str(exc.detail), "http_error")

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

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticated)])
    async def completion(payload: ChatRequest, request: Request):
        for header in request.headers:
            if header.startswith("x-carbon-") and header != "x-carbon-priority":
                return error(422, "Unsupported routing header", "unsupported_policy")
        if (request.headers.get("x-carbon-priority", "high") != "high"
                or "x-task-type" in request.headers or "x-max-carbon" in request.headers):
            return error(422, "Deferred policies require /api/jobs (planned for M3)", "unsupported_policy")
        request_id = "req_" + uuid4().hex
        with engine.begin() as conn:
            endpoints = conn.execute(text("SELECT * FROM endpoints ORDER BY latency_rank, id")).mappings().all()
            decision = interactive_decision(request_id, payload.model, endpoints)
            selected = next((e for e in endpoints if e["id"] == decision.selected_endpoint), None)
            conn.execute(text("INSERT INTO decisions VALUES (:id,:at,:data)"),
                         dict(id=request_id, at=utcnow(), data=decision.model_dump_json()))
            if selected:
                attempt_id = "attempt_" + uuid4().hex
                conn.execute(text("INSERT INTO attempts (id,request_id,endpoint_id,started_at,outcome,uncertain) "
                                  "VALUES (:id,:request,:endpoint,:at,'running',0)"),
                             dict(id=attempt_id, request=request_id, endpoint=selected["id"], at=utcnow()))
        if not selected:
            return error(503, "No eligible simulated endpoint for this model", "no_eligible_endpoint")
        outcome, usage, upstream_id, uncertain = "failed", None, None, False
        try:
            adapted = await execute(app.state.client, settings, selected["region"], payload, request_id)
            usage, upstream_id = adapted.usage, adapted.upstream_request_id
            outcome = "succeeded"
            result = JSONResponse(adapted.completion)
        except UpstreamFailure as exc:
            uncertain = exc.uncertain
            outcome = "unknown" if uncertain else "failed"
            result = error(exc.status, exc.message, exc.code)
            if exc.retry_after:
                result.headers["retry-after"] = exc.retry_after
        with engine.begin() as conn:
            conn.execute(text("UPDATE attempts SET ended_at=:at,outcome=:outcome,usage=:usage,"
                              "upstream_request_id=:upstream,uncertain=:uncertain WHERE id=:id"),
                         dict(at=utcnow(), outcome=outcome, usage=json.dumps(usage) if usage else None,
                              upstream=upstream_id, uncertain=uncertain, id=attempt_id))
        result.headers["x-greenroute-request-id"] = request_id
        result.headers["x-greenroute-region"] = selected["region"]
        result.headers["x-greenroute-mode"] = "demo"
        return result

    @app.get("/api/activity", dependencies=[Depends(authenticated)])
    async def activity(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT d.request_id,d.created_at,d.data,a.outcome,a.usage,a.uncertain "
                                     "FROM decisions d LEFT JOIN attempts a ON a.request_id=d.request_id "
                                     "ORDER BY d.created_at DESC,d.request_id LIMIT :limit OFFSET :offset"),
                                dict(limit=limit, offset=offset)).mappings().all()
        return {"mode": "demo", "items": [dict(request_id=r["request_id"], created_at=r["created_at"],
                    decision=json.loads(r["data"]), outcome=r["outcome"],
                    usage=json.loads(r["usage"]) if r["usage"] else None, uncertain=bool(r["uncertain"])) for r in rows]}

    return app
