import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from greenroute.batch_trial import BatchTrialPlan, run_batch_trial
from greenroute.clock import ManualClock
from greenroute.app import create_app
from greenroute.worker import Worker
from test_live_runtime import env

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def plan(**values):
    return BatchTrialPlan(trial_id='pilot1', allowed_regions=['east', 'west'], **values)


def test_budget_reserves_all_worker_attempts():
    assert plan().summary()['reserved_completion_tokens'] == 288
    with pytest.raises(ValidationError):
        plan(completion_token_budget=96)
    assert plan(requests=1, completion_token_budget=96)


def test_batch_cli_dry_run_never_needs_services(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts/run_trial.py'
    response = subprocess.run([sys.executable, str(script), '--kind', 'batch', '--trial-id', 'pilot1',
        '--allowed-region', 'east', '--completion-token-budget', '288'],
        cwd=tmp_path, capture_output=True, text=True)
    assert response.returncode == 0, response.stderr
    report = json.loads(response.stdout)
    assert report['network_requests'] == 0 and report['plan']['max_dispatches'] == 9


@pytest.mark.parametrize("dirty", [False, True])
def test_batch_runner_live_gateway_and_worker_with_mocked_providers(env, dirty):
    settings, engine, clock, state, transport = env
    if dirty:
        state["intensity_west"] = 500
    with TestClient(create_app(settings, transport, clock)) as gateway:
        async def run():
            async with httpx.AsyncClient(transport=transport) as upstream:
                worker = Worker(engine, settings, upstream, clock)
                async def progress(_):
                    await worker.tick()
                    await worker.drain()
                    if dirty and state['intensity_west'] == 500:
                        state['intensity_west'] = 80
                        gateway.portal.call(gateway.app.state.grid_poller.refresh)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app)) as client:
                    return await run_batch_trial(plan(mode='live', model='shared-model', max_carbon=150),
                        client, 'live-client-key', clock, sleep=progress)
        report = asyncio.run(run())
    assert report['status'] == 'completed' and report['submissions'] == 3
    assert all(j['status'] == 'succeeded' and j['provider_completion_id'] == 'provider-west' for j in report['jobs'])
    assert all(j['decision']['selected_endpoint'] == 'west' for j in report['jobs'])
    assert all(j['decision']['estimation_context']['mode'] == 'live' for j in report['jobs'])
    if dirty:
        assert 'waiting_for_carbon' in [t['state'] for t in report['jobs'][0]['transitions']]
    assert 'live-client-key' not in json.dumps(report)
    assert '"content"' not in json.dumps(report)


def mock_run(*, status='succeeded', post_code=202, mismatch=False, transport_failure=False):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/ready':
            return httpx.Response(200, json={'mode': 'demo'})
        if request.url.path == '/api/regions':
            return httpx.Response(200, json={'mode': 'demo', 'items': [dict(region='east', model='demo-model',
                healthy=True, locality='simulated')]})
        if request.method == 'POST':
            assert request.headers['idempotency-key'] == 'trial-pilot1-0'
            assert json.loads(request.content)['policy']['deadline_policy'] == 'expire'
            if transport_failure:
                raise httpx.ReadTimeout('secret-provider-error', request=request)
            return httpx.Response(post_code, json={'id': 'job_123'})
        return httpx.Response(200, json=dict(id='job_123', mode='live' if mismatch else 'demo', status=status,
            attempt_count=1, attempts=[dict(id='attempt1', request_id='request1', endpoint_id='east',
                outcome=status, uncertain=status == 'unknown')], decision={'mode': 'demo'},
            result={'id': 'provider1', 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_batch_trial(plan(max_polls=2), client, 'secret-key', ManualClock(NOW),
                                         sleep=lambda _: asyncio.sleep(0))
    return asyncio.run(run()), calls


@pytest.mark.parametrize('status', ['failed', 'expired', 'cancelled', 'unknown', 'waiting_for_carbon'])
def test_terminal_or_poll_limit_stops_further_submissions(status):
    report, calls = mock_run(status=status)
    assert report['status'] == 'stopped' and report['submissions'] == 1
    assert len([c for c in calls if c.method == 'POST']) == 1
    assert report['jobs'][0]['status'] == ('polling_stopped' if status == 'waiting_for_carbon' else status)
    assert report['polls'] <= 2


@pytest.mark.parametrize('code', [401, 409, 429, 500, 302])
def test_no_submission_retries_on_errors(code):
    report, calls = mock_run(post_code=code)
    assert report['status'] == 'stopped' and report['submissions'] == 1
    assert len(calls) == 3


def test_uncertain_submission_keeps_key_and_never_replays():
    report, calls = mock_run(transport_failure=True)
    assert report['jobs'][0]['status'] == 'submission_unknown'
    assert report['jobs'][0]['idempotency_key'] == 'trial-pilot1-0'
    assert report['submissions'] == 1
    assert 'secret-provider-error' not in json.dumps(report)


def test_wrong_mode_during_poll_stops_trial():
    report, _ = mock_run(mismatch=True)
    assert report['status'] == 'stopped'
    assert report['jobs'][0]['reason'] == 'job_identity_or_mode_mismatch'
