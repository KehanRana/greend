"""Offline dry run: wait -> select Frankfurt -> expire. No inference or job execution."""
import asyncio
from datetime import timedelta

from sqlalchemy import text
from greenroute.clock import ManualClock, SystemClock
from greenroute.config import Settings
from greenroute.contracts import JobPolicy
from greenroute.grid import FixtureGridAdapter, Snapshot
from greenroute.policy import carbon_decision
from greenroute.store import make_engine, ready


async def main():
    settings = Settings.from_env()
    engine = make_engine(settings.database)
    try:
        ready(engine)
        with engine.connect() as conn:
            endpoints = conn.execute(text("SELECT * FROM endpoints ORDER BY latency_rank,id")).mappings().all()
        clock = ManualClock(SystemClock().now())
        adapter = FixtureGridAdapter(clock)
        policy = JobPolicy.model_validate(dict(allowed_regions=[e['region'] for e in endpoints],
            max_carbon_gco2e_kwh=150,deadline_at=clock.now()+timedelta(seconds=200),deadline_policy='expire'),
            context={'now':clock.now()})
        for step in [0,180,20]:
            clock.advance(step)
            observations = await adapter.fetch([e['grid_zone'] for e in endpoints])
            snapshots = {o.zone:Snapshot(f'dry-run-{step}-{o.zone}',o.zone,o.intensity,o.observed_at,
                                         clock.now(),o.source) for o in observations}
            d=carbon_decision('dry-run','demo-model',endpoints,policy,snapshots,clock.now())
            print(d.model_dump_json(indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
