import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from greenroute.clock import ManualClock
from greenroute.trial import TrialPlan, run_trial

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def trial(*, mode='demo', signal='fresh', post_status=200, usage=None, malformed=False):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer secret-key'
        if request.url.path == '/ready':
            return httpx.Response(200, json={'mode': mode})
        if request.url.path == '/api/metrics':
            return httpx.Response(200, json={'mode': mode, 'configuration': {'baseline_endpoint': 'east'}})
        if request.url.path == '/api/regions':
            return httpx.Response(200, json={'mode': mode, 'items': [dict(endpoint_id='east',
                region='east', model='demo-model', healthy=True, locality='simulated', latency_rank=0,
                signal_status=signal, observed_at=NOW.isoformat(), snapshot_id='snapshot-123')]})
        assert request.url.path == '/v1/chat/completions'
        assert json.loads(request.content)['max_tokens'] == 32
        if malformed:
            return httpx.Response(200, text='secret-provider-error')
        return httpx.Response(post_status, json={'id': 'provider-id',
            'choices': [{'message': {'content': 'do-not-save-output'}}],
            'usage': usage if usage is not None else {'prompt_tokens': 15, 'completion_tokens': 8, 'total_tokens': 23}},
            headers={'x-greenroute-mode': mode, 'x-greenroute-region': 'east',
                     'x-greenroute-request-id': 'gateway-id'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await run_trial(TrialPlan(), client, 'secret-key', ManualClock(NOW))
    return asyncio.run(run()), calls


def test_dry_run_from_any_directory_needs_no_key_or_network(tmp_path, monkeypatch):
    monkeypatch.delenv('GREENROUTE_GATEWAY_KEY', raising=False)
    script = Path(__file__).resolve().parents[1] / 'scripts/run_trial.py'
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['status'] == 'dry_run' and report['network_requests'] == 0
    assert report['plan']['reserved_completion_tokens'] == 96


def test_success_is_bounded_and_evidence_omits_content_and_credentials():
    report, calls = trial()
    assert report['status'] == 'completed'
    assert report['dispatches'] == 3 and report['network_requests'] == 6
    assert report['reported_completion_tokens'] == 24
    assert all(e['provider_completion_id'] == 'provider-id' for e in report['executions'])
    assert [r.method for r in calls] == ['GET'] * 3 + ['POST'] * 3
    assert 'secret-key' not in json.dumps(report)
    assert 'do-not-save-output' not in json.dumps(report)


@pytest.mark.parametrize('options,reason', [({'mode': 'live'}, 'gateway_not_ready_or_mode_mismatch'),
    ({'signal': 'stale'}, 'fresh_comparison_signals_required')])
def test_preflight_failure_never_dispatches(options, reason):
    report, calls = trial(**options)
    assert report['status'] == 'preflight_failed' and report['reason'] == reason
    assert not any(r.method == 'POST' for r in calls)


@pytest.mark.parametrize('status', [401, 429, 500, 302])
def test_failure_stops_without_retry_or_redirect(status):
    report, calls = trial(post_status=status)
    assert report['status'] == 'stopped' and report['dispatches'] == 1
    assert len([r for r in calls if r.method == 'POST']) == 1


@pytest.mark.parametrize('usage', [{}, {'prompt_tokens': 1, 'completion_tokens': 33, 'total_tokens': 34},
    {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 99}])
def test_invalid_or_excess_usage_stops_trial(usage):
    report, _ = trial(usage=usage)
    assert report['status'] == 'stopped' and report['dispatches'] == 1
    assert report['executions'][0]['status'] == 'unknown'


def test_malformed_provider_response_is_sanitized():
    report, _ = trial(malformed=True)
    assert report['status'] == 'stopped'
    assert report['executions'][0]['status'] == 'unknown'
    assert 'secret-provider-error' not in json.dumps(report)


@pytest.mark.parametrize('changes', [{'requests': 4}, {'requests': 21}, {'max_tokens': 257},
    {'base_url': 'http://public.example.com'}, {'base_url': 'https://user:secret@example.com'},
    {'base_url': 'https://example.com/?key=secret'}])
def test_invalid_budget_and_destinations_are_rejected(changes):
    with pytest.raises(ValidationError):
        TrialPlan(**changes)


def test_transport_interruption_preserves_uncertainty():
    async def run():
        def handler(request):
            if request.method == 'POST':
                raise httpx.ReadTimeout('secret-text', request=request)
            if request.url.path == '/ready':
                return httpx.Response(200, json={'mode': 'demo'})
            if request.url.path == '/api/metrics':
                return httpx.Response(200, json={'mode': 'demo', 'configuration': {'baseline_endpoint': 'east'}})
            return httpx.Response(200, json={'mode': 'demo', 'items': [dict(endpoint_id='east',
                model='demo-model', healthy=True, locality='simulated', latency_rank=0, signal_status='fresh')]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_trial(TrialPlan(), client, 'secret-key', ManualClock(NOW))
    report = asyncio.run(run())
    assert report['dispatches'] == 1 and report['executions'][0]['status'] == 'unknown'
    assert 'secret-text' not in json.dumps(report)


from test_live_runtime import env


def test_runner_against_live_gateway_with_mocked_providers(env):
    from fastapi.testclient import TestClient
    from greenroute.app import create_app
    from sqlalchemy import text
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as gateway:
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app)) as client:
                return await run_trial(TrialPlan(mode='live', model='shared-model'), client,
                                       'live-client-key', clock)
        report = asyncio.run(run())
    assert report['status'] == 'completed' and report['dispatches'] == 3
    assert all(e['provider_completion_id'] == 'provider-east' for e in report['executions'])
    assert all(e['mode'] == 'live' for e in report['executions'])
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM attempts')).scalar() == 3
        assert conn.execute(text('SELECT COUNT(*) FROM jobs')).scalar() == 0
