"""Test-only gateway factory with an observable grid-source outage switch."""
import os
from dataclasses import replace
from pathlib import Path
from greenroute.app import create_app as normal_gateway
from greenroute.clock import SystemClock
from greenroute.config import Settings
from greenroute.grid import GridAdapterError
from greenroute.scenarios import DatabaseFixtureGridAdapter
from greenroute.store import make_engine


class FaultGrid(DatabaseFixtureGridAdapter):
    async def fetch(self,zones):
        if Path(os.environ['GREENROUTE_VERIFY_GRID_FAIL_FILE']).exists():
            raise GridAdapterError('Simulated source outage')
        observations=await super().fetch(zones)
        # This synthetic source emits a new observation each successful fetch.
        # Unlike the production fixture's minute cadence, it supports fast TTL tests.
        return [replace(o,observed_at=self.clock.now()) for o in observations]


def create_app():
    settings=Settings.from_env()
    clock=SystemClock()
    engine=make_engine(settings.database)
    adapter=FaultGrid(engine,clock)
    return normal_gateway(settings,clock=clock,grid_adapter=adapter)
