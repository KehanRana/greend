"""Persisted circuit state; configuration failures require operator intervention."""
from datetime import timedelta
from sqlalchemy import text
from greenroute.jobs import stamp
from greenroute.store import immediate


def record_outcome(engine, endpoint, failure, now):
    with immediate(engine) as conn:
        row = conn.execute(text('SELECT * FROM endpoints WHERE id=:id'),dict(id=endpoint)).mappings().first()
        if not row:
            return
        if failure is None:
            conn.execute(text('UPDATE endpoints SET consecutive_failures=0 WHERE id=:id AND disabled_reason IS NULL'),dict(id=endpoint))
        elif failure.disable_endpoint:
            conn.execute(text("UPDATE endpoints SET healthy=0,disabled_reason='configuration_error',probe_after=NULL WHERE id=:id"),dict(id=endpoint))
        elif failure.endpoint_failure and row['disabled_reason'] != 'configuration_error':
            count = row['consecutive_failures']+1
            conn.execute(text('UPDATE endpoints SET consecutive_failures=:count,healthy=:healthy,'
                              'disabled_reason=:reason,probe_after=:probe WHERE id=:id'),
                dict(id=endpoint,count=count,healthy=bool(row['healthy']) and count<3,
                     reason='circuit_open' if count>=3 else row['disabled_reason'],
                     probe=stamp(now+timedelta(seconds=30)) if count>=3 else row['probe_after']))


def enable(engine, endpoint):
    with immediate(engine) as conn:
        return conn.execute(text('UPDATE endpoints SET healthy=1,consecutive_failures=0,disabled_reason=NULL,'
                                 'probe_after=NULL WHERE id=:id'),dict(id=endpoint)).rowcount == 1
