import asyncio
from dataclasses import replace
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
from greenroute.grid import (FixtureGridAdapter, GridAdapterError, GridPoller, Observation,
                             Snapshot, latest_snapshots, signal_status)
from greenroute.store import make_engine

NOW = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)
KEY = 'grid-test-key'
HEADERS = {'authorization': f'Bearer {KEY}'}


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / 'grid.db'
    monkeypatch.setenv('GREENROUTE_DATABASE', str(path))
    monkeypatch.setenv('GREENROUTE_GATEWAY_KEY', KEY)
    command.upgrade(Config('alembic.ini'), 'head')
    engine = make_engine(path)
    yield path, engine
    engine.dispose()


def test_fixture_drop_and_durable_cache(database):
    path, engine = database
    clock = ManualClock(NOW)
    adapter = FixtureGridAdapter(clock)
    poller = GridPoller(engine, adapter, clock)
    assert asyncio.run(poller.refresh())
    first = latest_snapshots(engine)
    assert {k: v.intensity for k,v in first.items()} == {
        'demo:virginia':400, 'demo:frankfurt':300, 'demo:sydney':500}
    clock.advance(179)
    assert asyncio.run(poller.refresh())
    assert latest_snapshots(engine)['demo:frankfurt'].intensity == 300
    clock.advance(1)
    assert asyncio.run(poller.refresh())
    snapshots = latest_snapshots(engine)
    assert snapshots['demo:frankfurt'].intensity == 80
    assert snapshots['demo:frankfurt'].observed_at == NOW+timedelta(seconds=180)
    reopened = make_engine(path)
    assert latest_snapshots(reopened) == snapshots
    reopened.dispose()


def test_retrieval_does_not_rejuvenate_old_source_time(database):
    _, engine = database
    clock = ManualClock(NOW)
    class OldSource:
        async def fetch(self, zones):
            return [Observation(z, 20, NOW-timedelta(seconds=301), 'old-fixture') for z in zones]
    poller = GridPoller(engine, OldSource(), clock)
    assert asyncio.run(poller.refresh())
    snapshot = latest_snapshots(engine)['demo:virginia']
    assert snapshot.retrieved_at == NOW
    assert signal_status(snapshot, NOW) == 'stale'
    clock.advance(60)
    assert asyncio.run(poller.refresh())
    snapshot = latest_snapshots(engine)['demo:virginia']
    assert snapshot.observed_at == NOW-timedelta(seconds=301)
    assert signal_status(snapshot, clock.now()) == 'stale'


def test_failure_backoff_and_recovery_keep_cache(database):
    _, engine = database
    clock = ManualClock(NOW)
    poller = GridPoller(engine, FixtureGridAdapter(clock), clock)
    assert asyncio.run(poller.refresh())
    cached = latest_snapshots(engine)
    class Failing:
        async def fetch(self, zones):
            raise GridAdapterError('secret upstream credentials should not be logged')
    poller.adapter = Failing()
    for expected in [120, 240, 480, 900, 900]:
        assert not asyncio.run(poller.refresh())
        assert poller.next_delay_seconds == expected
        assert latest_snapshots(engine) == cached
        assert poller.metadata()['last_error'] == 'grid_refresh_failed'
    clock.advance(301)
    assert all(signal_status(s,clock.now()) == 'stale' for s in cached.values())
    poller.adapter = FixtureGridAdapter(clock)
    assert asyncio.run(poller.refresh())
    assert poller.failures == 0 and poller.next_delay_seconds == 60
    assert poller.metadata()['status'] == 'ok'


@pytest.mark.parametrize('value', [None, -1, float('nan'), float('inf'), -float('inf')])
def test_invalid_values_are_persisted_as_unusable(database, value):
    _, engine = database
    clock = ManualClock(NOW)
    class Invalid:
        async def fetch(self,zones):
            return [Observation(z,value,NOW,'invalid-fixture') for z in zones]
    poller = GridPoller(engine, Invalid(), clock)
    assert asyncio.run(poller.refresh())
    for snapshot in latest_snapshots(engine).values():
        assert snapshot.intensity is None
        assert signal_status(snapshot, NOW) == 'invalid'
        assert snapshot.quality_flags == ('invalid_intensity',)


def test_partial_batch_and_timeout_preserve_cache(database):
    _, engine = database
    clock = ManualClock(NOW)
    poller = GridPoller(engine, FixtureGridAdapter(clock), clock, timeout_seconds=.01)
    assert asyncio.run(poller.refresh())
    cached = latest_snapshots(engine)
    class Partial:
        async def fetch(self,zones):
            return [Observation(zones[0], 50, NOW, 'partial')]
    poller.adapter = Partial()
    assert not asyncio.run(poller.refresh())
    class Hanging:
        async def fetch(self,zones):
            await asyncio.Event().wait()
    poller.adapter = Hanging()
    assert not asyncio.run(poller.refresh())
    assert latest_snapshots(engine) == cached


@pytest.mark.parametrize('change,status', [
    ({'intensity':0},'fresh'),
    ({'observed_at':NOW-timedelta(seconds=300)},'fresh'),
    ({'observed_at':NOW-timedelta(seconds=300.001)},'stale'),
    ({'observed_at':NOW+timedelta(seconds=1)},'future'),
    ({'retrieved_at':NOW+timedelta(seconds=1)},'future'),
    ({'retrieved_at':NOW-timedelta(seconds=1)},'future'),
    ({'source':''},'invalid'), ({'mode':'live'},'invalid')])
def test_signal_quality_boundaries(change,status):
    snapshot = Snapshot('s','zone',100,NOW,NOW,'fixture')
    assert signal_status(replace(snapshot,**change),NOW) == status
    assert signal_status(None,NOW) == 'unavailable'


def test_poll_loop_cancels_cleanly(database, monkeypatch):
    _, engine = database
    clock = ManualClock(NOW)
    poller = GridPoller(engine, FixtureGridAdapter(clock), clock)
    delays = []
    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr('greenroute.grid.asyncio.sleep', fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poller.run())
    assert delays == [60,60]
    assert latest_snapshots(engine)


def test_regions_api_and_interactive_independence(database):
    path, engine = database
    clock = ManualClock(NOW)
    class Failing:
        calls = 0
        async def fetch(self,zones):
            self.calls += 1
            raise GridAdapterError('private error')
    adapter = Failing()
    settings = Settings(gateway_key=KEY,database=path)
    with TestClient(create_app(settings,clock=clock)) as client:
        assert client.get('/api/regions').status_code == 401
        data = client.get('/api/regions',headers=HEADERS).json()
        assert len(data['items']) == 3 and data['mode'] == 'demo'
        assert all(item['signal_status'] == 'fresh' for item in data['items'])
        assert all(item['source'] == 'demo-fixture-v1' for item in data['items'])
    def mock(request):
        return httpx.Response(200,json={'id':'chatcmpl-grid-outage','object':'chat.completion',
            'created':1,'model':'demo-model','choices':[{'index':0,'message':{'role':'assistant','content':'mock'},
                                                       'finish_reason':'stop'}]})
    clock.advance(301)
    with TestClient(create_app(settings,httpx.MockTransport(mock),clock,adapter)) as client:
        data = client.get('/api/regions',headers=HEADERS).json()
        assert data['poller']['status'] == 'degraded'
        assert all(item['signal_status'] == 'stale' for item in data['items'])
        assert 'private error' not in str(data)
        calls = adapter.calls
        r = client.post('/v1/chat/completions',headers=HEADERS,
            json={'model':'demo-model','messages':[{'role':'user','content':'hello'}]})
        assert r.status_code == 200 and r.headers['x-greenroute-region'] == 'us-east-1'
        assert adapter.calls == calls  # No source fetch on the interactive path.


def test_upgrade_preserves_m1_data(database, monkeypatch, tmp_path):
    path = tmp_path/'old.db'
    monkeypatch.setenv('GREENROUTE_DATABASE',str(path))
    command.upgrade(Config('alembic.ini'),'001')
    engine = make_engine(path)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO decisions VALUES ('old-request','2026-10-28T00:00:00Z','{}')"))
    command.upgrade(Config('alembic.ini'),'head')
    with engine.connect() as conn:
        assert conn.execute(text("SELECT request_id FROM decisions")).scalar() == 'old-request'
        assert conn.execute(text("SELECT COUNT(*) FROM endpoints WHERE grid_zone IS NOT NULL")).scalar() == 3
    engine.dispose()


def test_persisted_fixture_drives_wait_release_and_expiry(database):
    from greenroute.contracts import JobPolicy
    from greenroute.policy import carbon_decision
    _, engine = database
    clock = ManualClock(NOW)
    poller = GridPoller(engine, FixtureGridAdapter(clock), clock)
    with engine.connect() as conn:
        endpoints = conn.execute(text('SELECT * FROM endpoints')).mappings().all()
    policy = JobPolicy.model_validate(dict(allowed_regions=[e['region'] for e in endpoints],
        max_carbon_gco2e_kwh=150,deadline_at=NOW+timedelta(seconds=200),deadline_policy='expire'),
        context={'now':clock.now()})
    actions = []
    for advance in [0,180,20]:
        clock.advance(advance)
        assert asyncio.run(poller.refresh())
        d = carbon_decision('fixture-job','demo-model',endpoints,policy,latest_snapshots(engine),clock.now())
        actions.append(d.action)
        if d.action == 'select':
            assert d.selected_endpoint == 'eu-central-1'
    assert actions == ['wait','select','expire']


def test_future_quality_duplicate_batch_and_storage_api(database):
    path, engine = database
    clock = ManualClock(NOW)
    class FutureSource:
        async def fetch(self,zones):
            return [Observation(z,10,NOW+timedelta(seconds=1),'future-fixture') for z in zones]
    poller = GridPoller(engine,FutureSource(),clock)
    assert asyncio.run(poller.refresh())
    snapshots = latest_snapshots(engine)
    assert all('future_observation' in s.quality_flags for s in snapshots.values())
    class Duplicate:
        async def fetch(self,zones):
            return [Observation(zones[0],10,NOW,'duplicate') for _ in zones]
    poller.adapter=Duplicate()
    assert not asyncio.run(poller.refresh())
    assert latest_snapshots(engine) == snapshots
    settings = Settings(gateway_key=KEY,database=path.parent/'unmigrated.db')
    with TestClient(create_app(settings,clock=clock)) as client:
        assert client.get('/api/regions',headers=HEADERS).status_code == 503


def test_migration_downgrade_and_reupgrade(database):
    _, engine = database
    command.downgrade(Config('alembic.ini'),'001')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM endpoints')).scalar() == 3
    command.upgrade(Config('alembic.ini'),'head')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM endpoints WHERE grid_zone IS NOT NULL')).scalar() == 3
