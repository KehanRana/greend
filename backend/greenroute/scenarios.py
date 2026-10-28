"""Persistent demo grid scenarios; changing signals never deletes or resets jobs."""
from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy import text
from greenroute.grid import FixtureGridAdapter, Observation
from greenroute.jobs import stamp
from greenroute.store import immediate


def read_scenario(engine):
    with engine.connect() as conn:
        return dict(conn.execute(text('SELECT scenario,started_at,revision FROM demo_scenario WHERE id=1')).mappings().one())


def set_scenario(engine, scenario, now):
    with immediate(engine) as conn:
        conn.execute(text('UPDATE demo_scenario SET scenario=:scenario,started_at=:at,revision=:revision WHERE id=1'),
                     dict(scenario=scenario,at=stamp(now),revision=uuid4().hex))


class DatabaseFixtureGridAdapter(FixtureGridAdapter):
    def __init__(self, engine, clock):
        super().__init__(clock)
        self.engine=engine

    async def fetch(self, zones):
        with immediate(self.engine) as conn:
            row=conn.execute(text('SELECT * FROM demo_scenario WHERE id=1')).mappings().one()
            if row['started_at'] is None:
                started=self.clock.now()
                conn.execute(text('UPDATE demo_scenario SET started_at=:at WHERE id=1'),dict(at=stamp(started)))
            else:
                started=datetime.fromisoformat(row['started_at'])
        elapsed=max(0,(self.clock.now()-started).total_seconds())
        scenario=row['scenario']
        frame=self.fixture['frames'][0]
        if scenario == 'clean':
            frame=self.fixture['frames'][-1]
        elif scenario == 'scheduled':
            frame=max((f for f in self.fixture['frames'] if f['at_seconds']<=elapsed),key=lambda f:f['at_seconds'])
        interval=self.fixture['observation_interval_seconds']
        observed=started+timedelta(seconds=(elapsed//interval)*interval)
        source=self.fixture['source'] if scenario=='scheduled' else f"{self.fixture['source']}:{scenario}"
        return [Observation(zone,frame['intensities'][zone],observed,source) for zone in zones]
