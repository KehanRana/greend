import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from greenroute.clock import ManualClock
from greenroute.grid_access import check_access

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def payload(**changes):
    return dict(zone='DE', carbonIntensity=80, datetime=NOW.isoformat(),
                emissionFactorType='lifecycle', flowTraced=True, temporalGranularity='hourly') | changes


def run(handler, zones=('DE',), **kwargs):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await check_access(client, 'private-test-key', ManualClock(NOW), zones, **kwargs)
    return asyncio.run(check())


def response_handler(response):
    def handler(request):
        assert request.method == 'GET'
        assert request.url.host == 'api.electricitymaps.com'
        assert request.headers['auth-token'] == 'private-test-key'
        if request.url.path.endswith('/zones'):
            return httpx.Response(200, json={'DE': {'access': ['carbon-intensity']},
                                            'FR': {'access': []}})
        assert request.url.params['disableCallerLookup'] == 'true'
        assert request.url.params['zone'] == 'DE'
        return response
    return handler


def test_discovery_and_freshness_emit_only_safe_fields():
    report = run(response_handler(httpx.Response(200, json=payload(extra='sensitive-body'))))
    assert report['status'] == 'ok' and report['network_requests'] == 2
    assert report['accessible_zones'] == ['DE']
    assert report['zones'][0]['status'] == 'fresh'
    assert report['zones'][0]['observed_at'] == NOW.isoformat()
    assert report['writes_performed'] is False
    assert 'private-test-key' not in json.dumps(report)
    assert 'sensitive-body' not in json.dumps(report)


@pytest.mark.parametrize('changes,status', [({'datetime': '2026-10-27T00:00:00Z'}, 'stale'),
    ({'datetime': '2026-10-29T00:00:00Z'}, 'future'), ({'datetime': '2026-10-28T00:00:00'}, 'invalid_response'),
    ({'zone': 'FR'}, 'invalid_response'), ({'carbonIntensity': True}, 'invalid_response'),
    ({'carbonIntensity': -1}, 'invalid_response'), ({'flowTraced': False}, 'invalid_response')])
def test_quality_is_reported_separately_from_access(changes, status):
    report = run(response_handler(httpx.Response(200, json=payload(**changes))))
    assert report['status'] == 'attention_required'
    assert report['zones'][0]['declared_latest_access']
    assert report['zones'][0]['status'] == status


@pytest.mark.parametrize('code,status', [(401,'authentication_failed'), (403,'access_denied'),
    (404,'not_found'), (429,'rate_limited'), (500,'provider_error'), (302,'redirect_refused')])
def test_provider_errors_sanitized_and_not_retried(code, status):
    report = run(response_handler(httpx.Response(code, text='secret-error',
                    headers={'location': 'https://other.invalid/'})))
    assert report['zones'][0]['status'] == status
    assert report['network_requests'] == 2
    assert 'secret-error' not in json.dumps(report)


def test_unknown_zone_does_not_send_lookup_or_infer_region():
    report = run(response_handler(httpx.Response(200)), zones=('not-a-zone',))
    assert report['zones'][0]['status'] == 'unknown_zone'
    assert report['network_requests'] == 1


def test_discovery_only_never_generates_inference_requests():
    report = run(response_handler(httpx.Response(200)), zones=())
    assert report['status'] == 'ok'
    assert report['network_requests'] == 1
    assert report['zones'] == []
    assert not report['locality_verified']


def test_discovery_errors_stop_before_zone_requests():
    report = run(lambda r: httpx.Response(401, text='provider-secret'))
    assert report['status'] == 'failed' and report['discovery_status'] == 'authentication_failed'
    assert report['network_requests'] == 1
    assert report['zones'] == []


def test_timeout_and_invalid_json_are_sanitized():
    def timeout(request):
        raise httpx.ReadTimeout('provider-secret', request=request)
    assert run(timeout)['discovery_status'] == 'timeout'
    report = run(lambda r: httpx.Response(200, text='not-json-provider-secret'))
    assert report['discovery_status'] == 'invalid_response'
    assert 'provider-secret' not in json.dumps(report)


def test_dotenv_reader_never_executes_and_env_takes_precedence(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts/check_grid_access.py'
    spec = importlib.util.spec_from_file_location('grid_checker', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv(module.KEY_NAME, raising=False)
    dotenv = tmp_path / '.env'
    dotenv.write_text("UNRELATED=ignored\nexport GREENROUTE_ELECTRICITYMAPS_API_KEY='$(touch SHOULD_NOT_EXIST)'\n")
    assert module.credential(dotenv) == '$(touch SHOULD_NOT_EXIST)'
    assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()
    monkeypatch.setenv(module.KEY_NAME, 'environment-secret')
    assert module.credential(dotenv) == 'environment-secret'


def test_cli_missing_key_and_bad_file_have_no_traceback(tmp_path, monkeypatch):
    import subprocess
    import sys
    monkeypatch.delenv('GREENROUTE_ELECTRICITYMAPS_API_KEY', raising=False)
    script = Path(__file__).resolve().parents[1] / 'scripts/check_grid_access.py'
    for args, expected in [([], 'missing_credential'),
        (['--env-file', str(tmp_path / 'absent')], 'credential_file_unavailable')]:
        result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, cwd=tmp_path)
        assert result.returncode == 1
        assert json.loads(result.stdout)['status'] == expected
        assert result.stderr == ''
