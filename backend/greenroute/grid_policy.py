"""Explicit source granularity and independent scheduling freshness limits."""
from typing import Literal

Granularity = Literal['5_minutes', '15_minutes', 'hourly']
INTERVAL_SECONDS = {'5_minutes': 300, '15_minutes': 900, 'hourly': 3600}


def freshness_policy(granularity, max_age_seconds):
    interval = INTERVAL_SECONDS[granularity]
    return dict(temporal_granularity=granularity, nominal_interval_seconds=interval,
        max_age_seconds=max_age_seconds, timestamp_basis='source_datetime',
        warnings=(['freshness_window_shorter_than_source_interval'] if max_age_seconds < interval else []))
