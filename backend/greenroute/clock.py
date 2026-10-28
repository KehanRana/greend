"""UTC clocks shared by fixture polling and deterministic policy tests."""
from datetime import datetime, timedelta, timezone
from typing import Protocol


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    def __init__(self, now: datetime):
        self._now = require_aware(now)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float):
        if seconds < 0:
            raise ValueError("Clock cannot move backwards")
        self._now += timedelta(seconds=seconds)
