import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from greenroute.clock import ManualClock
from greenroute.contracts import ChatRequest
from greenroute.grid import GridAdapterError
from greenroute.inference import UpstreamFailure
from greenroute.live import Deployment, ElectricityMapsAdapter, LiveManifest, VLLMAdapter

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def deployment(**changes):
    return Deployment(**(dict(id='east', base_url='https://east.example.invalid/v1',
        api_key_env='VLLM_EAST_KEY', region='east', grid_zone='TEST-EAST',
        model_alias='shared-model', served_model='deployed-model', model_version='sha256:revision',
        locality_evidence='deployment-record-123') | changes))


def grid_data(**changes):
    return dict(zone='TEST-EAST', carbonIntensity=80, datetime=NOW.isoformat(),
        updatedAt='2099-01-01T00:00:00Z', emissionFactorType='lifecycle', flowTraced=True, temporalGranularity='hourly') | changes


def test_grid_preserves_source_time_and_explicit_provenance():
    async def run():
        def handler(request):
            assert request.url.host == 'api.electricitymaps.com'
            assert request.url.params['zone'] == 'TEST-EAST'
            assert request.url.params['disableCallerLookup'] == 'true'
            assert request.headers['auth-token'] == 'grid-secret'
            return httpx.Response(200, json=grid_data())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = ElectricityMapsAdapter(client, SecretStr('grid-secret'), ManualClock(NOW))
            result = await adapter.fetch(['TEST-EAST'])
            assert result[0].observed_at == NOW
            assert result[0].mode == 'live'
            assert result[0].intensity == 80
    asyncio.run(run())


@pytest.mark.parametrize('changes', [dict(zone='WRONG'), dict(carbonIntensity=-1),
    dict(carbonIntensity=True), dict(carbonIntensity='80'), dict(carbonIntensity=None),
    dict(datetime='2026-10-27T00:00:00Z'), dict(datetime='2026-10-29T00:00:00Z'),
    dict(datetime='2026-10-28T00:00:00'), dict(flowTraced=False), dict(emissionFactorType='direct')])
def test_invalid_grid_batch_fails_closed(changes):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=grid_data(**changes)))) as client:
            with pytest.raises(GridAdapterError, match='unavailable or invalid'):
                await ElectricityMapsAdapter(client, SecretStr('secret'), ManualClock(NOW)).fetch(['TEST-EAST'])
    asyncio.run(run())


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500, 302])
def test_grid_provider_errors_are_sanitized_without_retries(status):
    calls = []
    async def run():
        def handler(request):
            calls.append(request)
            return httpx.Response(status, text='sensitive-provider-body',
                                  headers={'location': 'https://other.invalid/'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with pytest.raises(GridAdapterError) as exc:
                await ElectricityMapsAdapter(client, SecretStr('secret'), ManualClock(NOW)).fetch(['TEST-EAST'])
            assert 'secret' not in str(exc.value)
            assert 'sensitive' not in str(exc.value)
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('url', ['http://east.invalid/v1', 'https://user:secret@east.invalid/v1',
    'https://east.invalid/v1?key=secret', 'https://east.invalid/v1#secret', 'https://east.invalid/'])
def test_manifest_rejects_unsafe_destinations(url):
    with pytest.raises(ValidationError):
        deployment(base_url=url)


def test_manifest_requires_fixed_baseline_and_equivalence():
    east = deployment()
    west = deployment(id='west', region='west')
    assert LiveManifest(baseline_endpoint='east', deployments=[east, west])
    for baseline, entries in [('missing', [east, west]), ('east', [east, east]),
                             ('east', [east, west.model_copy(update={'model_version': 'different'})])]:
        with pytest.raises(ValidationError):
            LiveManifest(baseline_endpoint=baseline, deployments=entries)


def test_vllm_maps_model_and_never_forwards_client_credentials(monkeypatch):
    monkeypatch.setenv('VLLM_EAST_KEY', 'regional-secret')
    calls = []
    async def run():
        def handler(request):
            calls.append(request)
            assert request.headers['authorization'] == 'Bearer regional-secret'
            if request.method == 'GET':
                return httpx.Response(200, json={'data': [{'id': 'deployed-model'}]})
            assert json.loads(request.content)['model'] == 'deployed-model'
            assert request.headers['x-greenroute-request-id'] == 'request-123'
            return httpx.Response(200, json={'id': 'provider-id', 'object': 'chat.completion',
                'choices': [{'message': {'content': 'answer'}}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = VLLMAdapter(client)
            assert (await adapter.probe(deployment()))['locality_verified'] is False
            result = await adapter.execute(deployment(), ChatRequest(model='shared-model',
                messages=[{'role': 'user', 'content': 'hello'}]), 'request-123')
            assert result.upstream_request_id == 'provider-id'
            assert result.usage['total_tokens'] == 3
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('status,code,uncertain', [(429, 'upstream_rate_limit', False),
    (500, 'upstream_error', True), (401, 'upstream_error', False), (302, 'upstream_error', False)])
def test_vllm_uses_existing_retry_contract(monkeypatch, status, code, uncertain):
    monkeypatch.setenv('VLLM_EAST_KEY', 'regional-secret')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                status, headers={'retry-after': '5', 'location': 'https://other.invalid/'})),
                follow_redirects=True) as client:
            with pytest.raises(UpstreamFailure) as exc:
                await VLLMAdapter(client).execute(deployment(), ChatRequest(model='shared-model',
                    messages=[{'role': 'user', 'content': 'hello'}]), 'request-123')
            assert exc.value.code == code
            assert exc.value.uncertain is uncertain
            if status == 429:
                assert exc.value.safe_to_retry and exc.value.retry_after == '5'
    asyncio.run(run())


def test_missing_credential_and_wrong_model_never_send(monkeypatch):
    monkeypatch.delenv('VLLM_EAST_KEY', raising=False)
    async def run():
        def unexpected(request):
            pytest.fail('No network request expected')
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            adapter = VLLMAdapter(client)
            with pytest.raises(ValueError, match='credential'):
                await adapter.probe(deployment())
            with pytest.raises(ValueError, match='alias'):
                await adapter.execute(deployment(), ChatRequest(model='wrong',
                    messages=[{'role': 'user', 'content': 'hello'}]), 'id')
    asyncio.run(run())


def test_offline_checker_is_runnable_and_sanitizes_invalid_input(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / 'scripts/check_live_config.py')]
    result = subprocess.run(command + [str(root / 'examples/live-endpoints.example.json')],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['network_requests'] == 0
    assert report['live_runtime_enabled'] is False
    invalid = tmp_path / 'bad.json'
    invalid.write_text('{"sensitive": "do-not-print-me"}')
    result = subprocess.run(command + [str(invalid)], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'do-not-print-me' not in result.stdout + result.stderr
    assert json.loads(result.stdout)['status'] == 'invalid_manifest'
