import json
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlalchemy import text

from greenroute.app import create_app
from greenroute.config import Settings

KEY = "test-gateway-key"
HEADERS = {"authorization": f"Bearer {KEY}"}
BODY = {"model": "demo-model", "messages": [{"role": "user", "content": "PRIVATE DOCUMENT"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("GREENROUTE_GATEWAY_KEY", KEY)
    monkeypatch.setenv("GREENROUTE_DATABASE", str(db))
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(gateway_key=KEY, database=db)
    seen = []

    def mock(request):
        seen.append(request)
        f = json.loads(Path("fixtures/summaries.json").read_text())
        return httpx.Response(200, json={"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
            "model": "demo-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": f["summary"]},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 32, "completion_tokens": 24, "total_tokens": 56}})
    return settings, httpx.MockTransport(mock), seen


def test_sdk_and_restart(setup):
    settings, transport, seen = setup
    app = create_app(settings, transport)
    with TestClient(app) as client:
        sdk = OpenAI(api_key=KEY, base_url="http://testserver/v1", http_client=client, max_retries=0)
        result = sdk.chat.completions.create(**BODY)
        assert result.choices[0].message.content.startswith("The document")
        assert client.get("/ready").status_code == 200
        activity = client.get("/api/activity", headers=HEADERS).json()
        assert activity["items"][0]["outcome"] == "succeeded"
        assert "PRIVATE DOCUMENT" not in json.dumps(activity)
        assert KEY not in json.dumps(activity)
        assert seen[0].headers["authorization"] == "Bearer demo-upstream-key"
    with TestClient(create_app(settings, transport)) as restarted:
        assert restarted.get("/api/activity", headers=HEADERS).json() == activity


@pytest.mark.parametrize("patch", [{"stream": True}, {"tools": []}, {"temperature": 3}, {"messages": []}])
def test_validation(setup, patch):
    settings, transport, _ = setup
    with TestClient(create_app(settings, transport)) as client:
        assert client.post("/v1/chat/completions", json=BODY | patch, headers=HEADERS).status_code == 422


def test_auth_limits_headers_fallback(setup):
    settings, transport, seen = setup
    app = create_app(settings, transport)
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=BODY).status_code == 401
        assert client.get("/api/activity").status_code == 401
        assert client.post("/v1/chat/completions", content=b"x" * (1024*1024+1), headers=HEADERS).status_code == 413
        for h in [{"x-carbon-priority": "async"}, {"x-max-carbon": "150"}, {"x-carbon-other": "bad"}]:
            assert client.post("/v1/chat/completions", json=BODY, headers=HEADERS | h).status_code == 422
        with app.state.engine.begin() as conn:
            conn.execute(text("UPDATE endpoints SET healthy=0 WHERE id='us-east-1'"))
        r = client.post("/v1/chat/completions", json=BODY, headers=HEADERS | {"x-private": "secret"})
        assert r.headers["x-greenroute-region"] == "eu-central-1"
        assert "x-private" not in seen[-1].headers
        with app.state.engine.begin() as conn:
            conn.execute(text("UPDATE endpoints SET healthy=0"))
        assert client.post("/v1/chat/completions", json=BODY, headers=HEADERS).status_code == 503


@pytest.mark.parametrize("kind,code", [("timeout",504),("rate",429),("bad",502)])
def test_upstream_errors(setup, kind, code):
    settings, _, _ = setup
    def failure(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("sensitive details", request=request)
        return httpx.Response(429 if kind == "rate" else 200, json={"secret": "private upstream details"})
    with TestClient(create_app(settings, httpx.MockTransport(failure))) as client:
        r = client.post("/v1/chat/completions", json=BODY, headers=HEADERS)
        assert r.status_code == code
        assert "sensitive" not in r.text and "private upstream" not in r.text
        assert client.get("/api/activity", headers=HEADERS).json()["items"][0]["outcome"] == (
            "unknown" if kind in {"timeout", "bad"} else "failed")


def test_constraints_and_readiness(setup, tmp_path):
    settings, transport, _ = setup
    app = create_app(settings, transport)
    from sqlalchemy.exc import IntegrityError
    with app.state.engine.begin() as conn:
        conn.execute(text("INSERT INTO jobs(id,owner_id,idempotency_key,body_digest,state) VALUES ('1','o','k','d','queued')"))
    with pytest.raises(IntegrityError), app.state.engine.begin() as conn:
        conn.execute(text("INSERT INTO jobs(id,owner_id,idempotency_key,body_digest,state) VALUES ('2','o','k','d','queued')"))
    with TestClient(create_app(settings.model_copy(update={"database":tmp_path/'unmigrated.db'}), transport)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
    assert settings.database.stat().st_mode & 0o777 == 0o600


def test_config():
    from pydantic import ValidationError
    for values in [{"gateway_key": "short"}, {"gateway_key": KEY, "mode": "live"},
                   {"gateway_key":KEY, "upstream_url":"https://untrusted.example"}]:
        with pytest.raises(ValidationError):
            Settings(**values)


def test_no_eligible_and_malformed(setup):
    settings, transport, _ = setup
    with TestClient(create_app(settings, transport)) as client:
        assert client.post("/v1/chat/completions", json=BODY | {"model":"unknown"}, headers=HEADERS).status_code == 503
        assert client.post("/v1/chat/completions", content=b'{broken',
                           headers=HEADERS | {"content-type":"application/json"}).status_code == 400


def test_job_policy_schema():
    from datetime import datetime, timezone, timedelta
    from greenroute.contracts import JobSubmission
    from pydantic import ValidationError
    policy = {"allowed_regions":["us-east-1"], "deadline_policy":"expire",
              "deadline_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    assert JobSubmission.model_validate({"request":BODY,"policy":policy}).policy.deadline_policy == 'expire'
    for patch in [{"deadline_policy":"unknown"}, {"deadline_at":"2020-01-01T00:00:00"},
                  {"allowed_regions":[]}, {"max_carbon_gco2e_kwh":-1}]:
        with pytest.raises(ValidationError):
            JobSubmission.model_validate({"request":BODY,"policy":policy | patch})
