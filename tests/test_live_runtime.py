"""Live runtime integration with synthetic HTTP providers; no external access."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from greenroute.app import create_app
from greenroute.clock import ManualClock
from greenroute.config import Settings
from greenroute.grid import latest_snapshots, GridPoller
from greenroute.live import ElectricityMapsAdapter
from greenroute.runtime import initialize_live, check_runtime
from greenroute.store import make_engine
from greenroute.worker import Worker

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
HEADERS = {'authorization': 'Bearer live-client-key'}
OPERATOR = {'authorization': 'Bearer live-operator-key'}
CHAT = {'model': 'shared-model', 'messages': [{'role': 'user', 'content': 'hello'}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / 'live.db'
    monkeypatch.setenv('GREENROUTE_DATABASE', str(path))
    monkeypatch.setenv('GREENROUTE_GATEWAY_KEY', 'migration-test-key')
    command.upgrade(Config('alembic.ini'), 'head')
    deployments = [dict(id=name, region=name, grid_zone=zone, model_alias='shared-model',
        served_model=f'model-{name}', model_version='immutable-revision-123',
        base_url=f'https://{name}.example.com/v1', api_key_env=f'VLLM_{name.upper()}_KEY',
        locality_evidence='reviewed-deployment-record-123', locality_verified=True,
        model_equivalence_verified=True) for name, zone in [('east', 'ZONE-E'), ('west', 'ZONE-W')]]
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(dict(baseline_endpoint='east', deployments=deployments)))
    for d in deployments:
        monkeypatch.setenv(d['api_key_env'], f"secret-{d['id']}")
    settings = Settings(mode='live', gateway_key='live-client-key', operator_key='live-operator-key',
        database=path, live_manifest=manifest, baseline_endpoint='east', electricitymaps_api_key='grid-secret')
    engine = make_engine(path)
    initialize_live(engine, settings)
    clock = ManualClock(NOW)
    state = dict(grid_status=200, inference_status=200, intensity_east=400, intensity_west=80,
                 observed_at=NOW.isoformat(), calls=[])

    def handler(request):
        state['calls'].append(request)
        if request.url.host == 'api.electricitymaps.com':
            assert request.headers['auth-token'] == 'grid-secret'
            if state['grid_status'] != 200:
                return httpx.Response(state['grid_status'], text='sensitive-grid-error')
            zone = request.url.params['zone']
            return httpx.Response(200, json=dict(zone=zone,
                carbonIntensity=state['intensity_east' if zone == 'ZONE-E' else 'intensity_west'],
                datetime=state['observed_at'], flowTraced=True, emissionFactorType='lifecycle'))
        name = request.url.host.split('.')[0]
        assert request.headers['authorization'] == f'Bearer secret-{name}'
        if request.method == 'GET':
            return httpx.Response(state['inference_status'], json={'data': [{'id': f'model-{name}'}]})
        assert json.loads(request.content)['model'] == f'model-{name}'
        if state['inference_status'] != 200:
            return httpx.Response(state['inference_status'], headers={'retry-after': '2'})
        return httpx.Response(200, json={'id': 'provider-' + name, 'object': 'chat.completion',
            'model': f'model-{name}', 'choices': [{'message': {'content': 'answer'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
    yield settings, engine, clock, state, httpx.MockTransport(handler)
    engine.dispose()


def job(clock, threshold=150):
    return dict(request=CHAT, policy=dict(allowed_regions=['east', 'west'],
        max_carbon_gco2e_kwh=threshold, deadline_policy='expire', deadline_at=(clock.now()+timedelta(minutes=10)).isoformat()))


def cycle(engine, settings, clock, transport):
    async def run():
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            worker = Worker(engine, settings, client, clock)
            await worker.tick()
            await worker.drain()
    asyncio.run(run())


def test_live_gateway_and_carbon_worker_have_isolated_provenance(env):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        assert client.get('/ready').json()['mode'] == 'live'
        assert client.get('/api/regions').status_code == 401
        response = client.post('/v1/chat/completions', headers=HEADERS, json=CHAT)
        assert response.status_code == 200
        assert response.headers['x-greenroute-mode'] == 'live'
        assert response.headers['x-greenroute-region'] == 'east'
        submitted = client.post('/api/jobs', headers=HEADERS | {'idempotency-key': 'live-job'}, json=job(clock))
        assert submitted.status_code == 202
        jid = submitted.json()['id']
        cycle(engine, settings, clock, transport)
        result = client.get(f'/api/jobs/{jid}', headers=HEADERS).json()
        assert result['status'] == 'succeeded' and result['mode'] == 'live'
        assert result['decision']['selected_endpoint'] == 'west'
        assert result['decision']['mode'] == 'live'
        data = client.get('/api/metrics', headers=HEADERS).json()
        assert data['mode'] == 'live' and data['compared_executions'] == 2
        assert data['owner_job_states'] == {'succeeded': 1}
        assert client.get('/api/metrics?mode=demo', headers=HEADERS).json()['succeeded_executions'] == 0
        estimates = client.get('/api/activity', headers=HEADERS).json()
        for row in estimates['items']:
            assert row['estimate']['usage_provenance'] == 'provider_reported_usage'
            assert row['estimate']['assumptions']['energy_is_illustrative']
            assert row['estimate']['assumptions']['model_version'] == 'immutable-revision-123'
        assert client.post('/api/demo/scenario', headers=OPERATOR, json={'scenario': 'clean'}).status_code == 404
        assert client.get('/api/demo/scenario', headers=HEADERS).status_code == 404
        assert client.get('/dashboard/').status_code == 404
        serialized = json.dumps(estimates) + json.dumps(client.get('/api/regions', headers=HEADERS).json())
        assert 'secret-' not in serialized and 'https://' not in serialized
    assert not latest_snapshots(engine)
    assert all(s.mode == 'live' for s in latest_snapshots(engine, 'live').values())


def test_waits_for_clean_grid_then_dispatches(env):
    settings, engine, clock, state, transport = env
    state['intensity_west'] = 500
    with TestClient(create_app(settings, transport, clock)) as client:
        jid = client.post('/api/jobs', headers=HEADERS | {'idempotency-key': 'waiting'}, json=job(clock)).json()['id']
        cycle(engine, settings, clock, transport)
        assert client.get(f'/api/jobs/{jid}', headers=HEADERS).json()['status'] == 'waiting_for_carbon'
        assert not [r for r in state['calls'] if r.method == 'POST']
        state['intensity_west'] = 80
        assert client.portal.call(client.app.state.grid_poller.refresh)
        cycle(engine, settings, clock, transport)
        assert client.get(f'/api/jobs/{jid}', headers=HEADERS).json()['status'] == 'succeeded'


@pytest.mark.parametrize('status', [401, 429, 503])
def test_grid_failure_retains_cache_then_stale_jobs_wait(env, status):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        before = latest_snapshots(engine, 'live')
        state['grid_status'] = status
        clock.advance(301)
        assert not client.portal.call(client.app.state.grid_poller.refresh)
        assert latest_snapshots(engine, 'live') == before
        regions = client.get('/api/regions', headers=HEADERS).json()
        assert all(r['signal_status'] == 'stale' for r in regions['items'])
        jid = client.post('/api/jobs', headers=HEADERS | {'idempotency-key': 'stale'}, json=job(clock)).json()['id']
        cycle(engine, settings, clock, transport)
        assert client.get(f'/api/jobs/{jid}', headers=HEADERS).json()['status'] == 'waiting_for_carbon'
        # Latency-critical requests still route without a carbon signal.
        assert client.post('/v1/chat/completions', headers=HEADERS, json=CHAT).status_code == 200
        assert client.get('/api/metrics', headers=HEADERS).json()['compared_executions'] == 0


@pytest.mark.parametrize('status,expected', [(401, 'failed'), (429, 'retry_pending'), (503, 'unknown')])
def test_live_job_provider_failure_contract(env, status, expected):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        state['inference_status'] = status
        jid = client.post('/api/jobs', headers=HEADERS | {'idempotency-key': 'failure'}, json=job(clock)).json()['id']
        cycle(engine, settings, clock, transport)
        result = client.get(f'/api/jobs/{jid}', headers=HEADERS).json()
        assert result['status'] == expected
        if status == 401:
            with engine.connect() as conn:
                assert conn.execute(text("SELECT disabled_reason FROM endpoints WHERE id='west'")).scalar() == 'configuration_error'
        if status == 429:
            state['inference_status'] = 200
            cycle(engine, settings, clock, transport)
            assert client.get(f'/api/jobs/{jid}', headers=HEADERS).json()['status'] == 'retry_pending'
            clock.advance(2)
            cycle(engine, settings, clock, transport)
            assert client.get(f'/api/jobs/{jid}', headers=HEADERS).json()['status'] == 'succeeded'


def test_runtime_rejects_mode_and_registry_drift(env):
    settings, engine, clock, state, transport = env
    demo = Settings(gateway_key='demo-client-key', database=settings.database)
    with pytest.raises(ValueError, match='identity'):
        check_runtime(engine, demo)
    with pytest.raises(ValueError, match='identity'):
        with TestClient(create_app(demo, transport, clock)):
            pass
    with engine.begin() as conn:
        conn.execute(text("UPDATE endpoints SET locality='unverified' WHERE id='west'"))
    with pytest.raises(ValueError, match='registry'):
        check_runtime(engine, settings)
    with pytest.raises(ValueError, match='registry'):
        Worker(engine, settings, httpx.AsyncClient(transport=transport), clock)


def test_approval_and_manifest_drift_fail_closed(env):
    settings, engine, clock, state, transport = env
    manifest = json.loads(settings.live_manifest.read_text())
    manifest['deployments'][0]['locality_verified'] = False
    settings.live_manifest.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='reviewed'):
        check_runtime(engine, settings)
    manifest['deployments'][0]['locality_verified'] = True
    manifest['deployments'][0]['base_url'] = 'https://changed.example.com/v1'
    settings.live_manifest.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='identity'):
        check_runtime(engine, settings)


def test_cannot_initialize_database_with_demo_evidence(env):
    settings, engine, clock, state, transport = env
    with engine.begin() as conn:
        conn.execute(text("UPDATE runtime_identity SET mode='demo',manifest_digest=NULL"))
        conn.execute(text("INSERT INTO decisions(request_id,created_at,data) VALUES ('old',:at,'{}')"),
                     dict(at=NOW.isoformat()))
    with pytest.raises(ValueError, match='empty'):
        initialize_live(engine, settings)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM decisions")).scalar() == 1


def test_live_circuit_probe_requires_the_registered_model(env):
    settings, engine, clock, state, transport = env
    with engine.begin() as conn:
        conn.execute(text("UPDATE endpoints SET healthy=0,disabled_reason='circuit_open',"
                          "consecutive_failures=3,probe_after=:at WHERE id='west'"), dict(at=NOW.isoformat()))
    state['inference_status'] = 401
    cycle(engine, settings, clock, transport)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT healthy FROM endpoints WHERE id='west'")).scalar() == 0
    state['inference_status'] = 200
    clock.advance(30)
    cycle(engine, settings, clock, transport)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT healthy FROM endpoints WHERE id='west'")).scalar() == 1
    assert all(r.method == 'GET' for r in state['calls'])


def test_manifest_is_bound_for_process_lifetime_but_changes_reject_on_restart(env):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        data = json.loads(settings.live_manifest.read_text())
        data['deployments'][0]['base_url'] = 'https://changed.example.com/v1'
        settings.live_manifest.write_text(json.dumps(data))
        assert client.post('/v1/chat/completions', headers=HEADERS, json=CHAT).status_code == 200
        assert all(r.url.host != 'changed.example.com' for r in state['calls'])
    restarted = Settings(**settings.model_dump())
    with pytest.raises(ValueError, match='identity'):
        check_runtime(engine, restarted)


def test_live_startup_and_worker_reject_uninitialized_database(env):
    settings, engine, clock, state, transport = env
    with engine.begin() as conn:
        conn.execute(text("UPDATE runtime_identity SET mode='demo',manifest_digest=NULL"))
    with pytest.raises(ValueError, match='identity'):
        with TestClient(create_app(settings, transport, clock)):
            pass
    async def run():
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(ValueError, match='identity'):
                Worker(engine, settings, http, clock)
    asyncio.run(run())
    assert state['calls'] == []


def test_live_mixed_mode_snapshots_never_displace_live_cache(env):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO grid_snapshots(id,zone,intensity,observed_at,retrieved_at,source,mode,quality_flags) "
                "VALUES ('wrong-mode','ZONE-W',0,:at,:at,'fixture','demo','[]')"), dict(at=NOW.isoformat()))
        jid = client.post('/api/jobs', headers=HEADERS | {'idempotency-key': 'mixed'}, json=job(clock, threshold=10)).json()['id']
        cycle(engine, settings, clock, transport)
        result = client.get(f'/api/jobs/{jid}', headers=HEADERS).json()
        assert result['status'] == 'waiting_for_carbon'
        assert result['decision']['reason'] == 'threshold_not_met'
        assert latest_snapshots(engine, 'live')['ZONE-W'].intensity == 80
        assert latest_snapshots(engine)['ZONE-W'].intensity == 0


def test_live_registry_drift_blocks_running_api_before_dispatch(env):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        with engine.begin() as conn:
            conn.execute(text("UPDATE endpoints SET model_version='unreviewed' WHERE id='west'"))
        response = client.post('/v1/chat/completions', headers=HEADERS, json=CHAT)
        assert response.status_code == 503
        assert not any(r.method == 'POST' for r in state['calls'])
        assert client.post('/api/operator/endpoints/west/enable', headers=OPERATOR).status_code == 503


def test_upgrade_preserves_existing_demo_evidence(tmp_path, monkeypatch):
    path = tmp_path / 'existing-demo.db'
    monkeypatch.setenv('GREENROUTE_DATABASE', str(path))
    monkeypatch.setenv('GREENROUTE_GATEWAY_KEY', 'migration-demo-key')
    command.upgrade(Config('alembic.ini'), '004')
    engine = make_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO decisions(request_id,created_at,data) VALUES ('existing',:at,'{}')"),
                     dict(at=NOW.isoformat()))
    command.upgrade(Config('alembic.ini'), 'head')
    settings = Settings(gateway_key='migration-demo-key', database=path)
    assert check_runtime(engine, settings) is None
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM decisions')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM endpoints')).scalar() == 3
    engine.dispose()
