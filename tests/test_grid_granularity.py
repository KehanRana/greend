import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from fastapi.testclient import TestClient

from greenroute.clock import ManualClock
from greenroute.config import Settings
from greenroute.grid import GridAdapterError, Snapshot, signal_status, latest_snapshots
from greenroute.grid_access import check_access
from greenroute.grid_policy import freshness_policy
from greenroute.live import ElectricityMapsAdapter
from greenroute.worker import Worker
from greenroute.app import create_app
from test_live_runtime import env, job, HEADERS

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def test_default_policy_warns_without_relaxing_freshness():
    config = Settings(gateway_key='granularity-key')
    assert config.grid_temporal_granularity == 'hourly'
    assert config.grid_max_age_seconds == 300
    assert freshness_policy('hourly', 300)['warnings'] == ['freshness_window_shorter_than_source_interval']
    assert freshness_policy('hourly', 7200)['warnings'] == []
    assert freshness_policy('5_minutes', 300)['warnings'] == []
    with pytest.raises(ValidationError):
        Settings(gateway_key='granularity-key', grid_temporal_granularity='daily')
    with pytest.raises(ValidationError):
        Settings(gateway_key='granularity-key', grid_max_age_seconds=7201)


@pytest.mark.parametrize('granularity', ['5_minutes', '15_minutes', 'hourly'])
def test_adapter_requests_and_preserves_exact_granularity(granularity):
    async def run():
        def handler(request):
            assert request.url.params['temporalGranularity'] == granularity
            return httpx.Response(200, json=dict(zone='DE', carbonIntensity=100, datetime=NOW.isoformat(),
                temporalGranularity=granularity, flowTraced=True, emissionFactorType='lifecycle'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = ElectricityMapsAdapter(client, SecretStr('key'), ManualClock(NOW),
                                             temporal_granularity=granularity)
            assert (await adapter.fetch(['DE']))[0].temporal_granularity == granularity
    asyncio.run(run())


@pytest.mark.parametrize('returned', [None, '5_minutes', 'daily'])
def test_adapter_rejects_missing_or_changed_granularity(returned):
    async def run():
        def handler(request):
            return httpx.Response(200, json=dict(zone='DE', carbonIntensity=100, datetime=NOW.isoformat(),
                temporalGranularity=returned, flowTraced=True, emissionFactorType='lifecycle'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(GridAdapterError):
                await ElectricityMapsAdapter(client, SecretStr('key'), ManualClock(NOW)).fetch(['DE'])
    asyncio.run(run())


def test_checker_reports_mismatch_without_fresh_label():
    async def run():
        def handler(request):
            if request.url.path.endswith('/zones'):
                return httpx.Response(200, json={'DE': {'access': ['carbon-intensity']}})
            assert request.url.params['temporalGranularity'] == '15_minutes'
            return httpx.Response(200, json=dict(zone='DE', carbonIntensity=100, datetime=NOW.isoformat(),
                temporalGranularity='hourly', flowTraced=True, emissionFactorType='lifecycle'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_access(client, 'key', ManualClock(NOW), ['DE'], temporal_granularity='15_minutes')
    report = asyncio.run(run())
    assert report['zones'][0]['status'] == 'granularity_mismatch'
    assert report['grid_policy']['temporal_granularity'] == '15_minutes'
    assert report['status'] == 'attention_required'


def test_exact_freshness_boundary_and_granularity_provenance():
    snapshot = Snapshot('id', 'zone', 100, NOW, NOW, 'source', mode='live', temporal_granularity='hourly')
    assert signal_status(snapshot, NOW, 300, 'live', 'hourly') == 'fresh'
    assert signal_status(snapshot, NOW, 300, 'live', '5_minutes') == 'invalid'
    assert signal_status(replace(snapshot, temporal_granularity=None), NOW, 300, 'live', 'hourly') == 'invalid'


def test_cache_filters_granularity_before_selecting_latest(env):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO grid_snapshots(id,zone,intensity,observed_at,retrieved_at,source,mode,quality_flags,temporal_granularity) "
                "VALUES ('different-granularity','ZONE-W',0,:at,:at,'electricitymaps','live','[]','5_minutes')"), dict(at=NOW.isoformat()))
        assert latest_snapshots(engine, 'live', 'hourly')['ZONE-W'].intensity == 80
        assert latest_snapshots(engine, 'live', '5_minutes')['ZONE-W'].intensity == 0
        metadata = client.get('/api/regions', headers=HEADERS).json()
        assert metadata['grid_policy']['max_age_seconds'] == 300
        assert metadata['grid_policy']['warnings']
        assert all(r['temporal_granularity'] == 'hourly' for r in metadata['items'])
        jid = client.post('/api/jobs', headers=HEADERS | {'idempotency-key': 'granularity'}, json=job(clock, threshold=10)).json()['id']
        async def run():
            async with httpx.AsyncClient(transport=transport) as upstream:
                worker = Worker(engine, settings, upstream, clock)
                await worker.tick()
                await worker.drain()
        asyncio.run(run())
        result = client.get('/api/jobs/' + jid, headers=HEADERS).json()
        assert result['status'] == 'waiting_for_carbon'
        assert result['decision']['reason'] == 'threshold_not_met'


def test_live_dispatch_captures_policy_and_source_granularity(env):
    settings, engine, clock, state, transport = env
    with TestClient(create_app(settings, transport, clock)) as client:
        response = client.post('/v1/chat/completions', headers=HEADERS,
                               json={'model': 'shared-model', 'messages': [{'role': 'user', 'content': 'hello'}]})
        assert response.status_code == 200
        context = client.get('/api/activity', headers=HEADERS).json()['items'][0]['decision']['estimation_context']
        assert context['grid_temporal_granularity'] == 'hourly'
        assert context['grid_max_age_seconds'] == 300
        assert all(s['temporal_granularity'] == 'hourly' for s in context['signals'].values())


def test_migration_does_not_guess_historical_granularity(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from greenroute.store import make_engine
    path = tmp_path / 'old.db'
    monkeypatch.setenv('GREENROUTE_DATABASE', str(path))
    monkeypatch.setenv('GREENROUTE_GATEWAY_KEY', 'granularity-migration-key')
    command.upgrade(Config('alembic.ini'), '005')
    engine = make_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO grid_snapshots(id,zone,intensity,observed_at,retrieved_at,source,mode,quality_flags) "
            "VALUES ('legacy','DE',80,:at,:at,'provider','live','[]')"), dict(at=NOW.isoformat()))
    command.upgrade(Config('alembic.ini'), 'head')
    assert latest_snapshots(engine, 'live')['DE'].temporal_granularity is None
    assert latest_snapshots(engine, 'live', 'hourly') == {}
    engine.dispose()
