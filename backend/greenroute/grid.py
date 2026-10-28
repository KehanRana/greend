"""Timestamped demo grid adapter, durable cache and bounded polling loop."""
import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sqlalchemy import text

from greenroute.clock import Clock, require_aware

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Observation:
    zone: str
    intensity: float | None
    observed_at: datetime
    source: str
    mode: str = "demo"


@dataclass(frozen=True)
class Snapshot:
    id: str
    zone: str
    intensity: float | None
    observed_at: datetime
    retrieved_at: datetime
    source: str
    mode: str = "demo"
    quality_flags: tuple[str, ...] = ()


def signal_status(snapshot: Snapshot | None, now: datetime, max_age_seconds=300) -> str:
    now = require_aware(now)
    if snapshot is None:
        return "unavailable"
    if (snapshot.quality_flags or snapshot.intensity is None
            or not math.isfinite(snapshot.intensity) or snapshot.intensity < 0
            or not snapshot.source or snapshot.mode != "demo"):
        return "invalid"
    observed = require_aware(snapshot.observed_at)
    retrieved = require_aware(snapshot.retrieved_at)
    # No skew allowance in the deterministic demo: future source times cannot qualify.
    if observed > now or retrieved > now or observed > retrieved:
        return "future"
    return "stale" if (now - observed).total_seconds() > max_age_seconds else "fresh"


class GridAdapterError(Exception):
    """A source returned no usable observation batch; keep the durable cache."""


class GridAdapter(Protocol):
    async def fetch(self, zones: list[str]) -> list[Observation]: ...


class FixtureGridAdapter:
    def __init__(self, clock: Clock, path: Path | None = None):
        self.clock = clock
        self.started_at = clock.now()
        path = path or Path(__file__).resolve().parents[2] / "fixtures" / "grid.json"
        self.fixture = json.loads(path.read_text())
        if not self.fixture["frames"] or self.fixture["observation_interval_seconds"] <= 0:
            raise ValueError("Invalid grid fixture")

    async def fetch(self, zones: list[str]) -> list[Observation]:
        elapsed = max(0, (self.clock.now() - self.started_at).total_seconds())
        frames = [f for f in self.fixture["frames"] if f["at_seconds"] <= elapsed]
        frame = max(frames, key=lambda f: f["at_seconds"])
        interval = self.fixture["observation_interval_seconds"]
        observed = self.started_at + timedelta(seconds=(elapsed // interval) * interval)
        try:
            return [Observation(zone, frame["intensities"][zone], observed, self.fixture["source"])
                    for zone in zones]
        except KeyError:
            raise GridAdapterError("Fixture has no approved mapping for requested zone") from None


def latest_snapshots(engine) -> dict[str, Snapshot]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
            "(PARTITION BY zone ORDER BY retrieved_at DESC, rowid DESC) AS rank "
            "FROM grid_snapshots) WHERE rank=1")).mappings().all()
    return {r["zone"]: Snapshot(id=r["id"], zone=r["zone"], intensity=r["intensity"],
        observed_at=datetime.fromisoformat(r["observed_at"]),
        retrieved_at=datetime.fromisoformat(r["retrieved_at"]), source=r["source"],
        mode=r["mode"], quality_flags=tuple(json.loads(r["quality_flags"] or "[]"))) for r in rows}


class GridPoller:
    def __init__(self, engine, adapter: GridAdapter, clock: Clock, refresh_seconds=60,
                 max_age_seconds=300, timeout_seconds=5):
        self.engine, self.adapter, self.clock = engine, adapter, clock
        self.refresh_seconds = refresh_seconds
        self.max_age_seconds = max_age_seconds
        self.timeout_seconds = timeout_seconds
        self.failures = 0
        self.last_error: str | None = None
        self.last_success_at: datetime | None = None

    @property
    def next_delay_seconds(self):
        # 60, 120, 240, ... up to 15 minutes on source/storage outages.
        return min(self.refresh_seconds * 2 ** min(self.failures, 8), 900)

    async def refresh(self) -> bool:
        try:
            with self.engine.connect() as conn:
                zones = list(conn.execute(text("SELECT DISTINCT grid_zone FROM endpoints "
                                               "WHERE grid_zone IS NOT NULL ORDER BY grid_zone")).scalars())
            async with asyncio.timeout(self.timeout_seconds):
                observations = await self.adapter.fetch(zones)
            if (len(observations) != len(zones) or {o.zone for o in observations} != set(zones)):
                raise GridAdapterError("Incomplete or duplicate observation batch")
            retrieved = self.clock.now()
            with self.engine.begin() as conn:
                for observation in observations:
                    intensity = observation.intensity
                    flags = []
                    if intensity is None or not math.isfinite(intensity) or intensity < 0:
                        intensity = None
                        flags.append("invalid_intensity")
                    observed = require_aware(observation.observed_at)
                    if observed > retrieved:
                        flags.append("future_observation")
                    if not observation.source or observation.mode != "demo":
                        flags.append("invalid_provenance")
                    conn.execute(text("INSERT INTO grid_snapshots "
                        "(id,zone,intensity,observed_at,retrieved_at,source,mode,quality_flags) "
                        "VALUES (:id,:zone,:intensity,:observed,:retrieved,:source,:mode,:flags)"),
                        dict(id="grid_" + uuid4().hex, zone=observation.zone, intensity=intensity,
                             observed=observed.isoformat(), retrieved=retrieved.isoformat(),
                             source=observation.source, mode=observation.mode, flags=json.dumps(flags)))
            self.failures, self.last_error, self.last_success_at = 0, None, retrieved
            return True
        except Exception:
            # Do not emit external exception strings: provider errors can contain credentials.
            self.failures += 1
            self.last_error = "grid_refresh_failed"
            logger.warning("Grid refresh failed; retaining timestamped cache")
            return False

    async def run(self):
        # The application performs the initial refresh before starting this loop.
        while True:
            await asyncio.sleep(self.next_delay_seconds)
            await self.refresh()

    def metadata(self):
        return {"status": "degraded" if self.last_error else "ok", "last_error": self.last_error,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                "consecutive_failures": self.failures, "next_poll_delay_seconds": self.next_delay_seconds}
