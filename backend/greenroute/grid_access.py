"""Bounded, read-only Electricity Maps diagnostics; no database or inference access."""
import asyncio
import math
from datetime import datetime

import httpx
from greenroute.clock import require_aware
from greenroute.grid_policy import freshness_policy, INTERVAL_SECONDS

BASE = 'https://api.electricitymaps.com/v4'


def failure(status):
    return {401: 'authentication_failed', 403: 'access_denied', 404: 'not_found',
            429: 'rate_limited'}.get(status, 'redirect_refused' if 300 <= status < 400 else 'provider_error')


async def check_access(client, key, clock, zones=(), timeout_seconds=10, max_age_seconds=300, temporal_granularity="hourly"):
    if temporal_granularity not in INTERVAL_SECONDS:
        raise ValueError('Unsupported grid granularity')
    report = dict(status='failed', api_version='v4', checked_at=clock.now().isoformat(),
        network_requests=0, discovery_status='not_checked', accessible_zones=[], zones=[],
        grid_policy=freshness_policy(temporal_granularity,max_age_seconds),
        max_age_seconds=max_age_seconds, writes_performed=False, locality_verified=False)
    if not key.strip():
        return report | {'status': 'missing_credential'}

    async def get(path, params=None):
        report['network_requests'] += 1
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await client.get(BASE + path, params=params,
                    headers={'auth-token': key}, follow_redirects=False)
            if response.status_code != 200:
                return None, failure(response.status_code)
            return response.json(), 'ok'
        except (httpx.TimeoutException, TimeoutError):
            return None, 'timeout'
        except httpx.RequestError:
            return None, 'connection_failed'
        except ValueError:
            return None, 'invalid_response'

    data, status = await get('/zones')
    report['discovery_status'] = status
    if status != 'ok':
        return report
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, dict)
                                         for k, v in data.items()):
        report['discovery_status'] = 'invalid_response'
        return report
    # Only emit zone IDs and interpreted permissions, never raw provider bodies.
    for zone, metadata in sorted(data.items()):
        access = metadata.get('access', [])
        if isinstance(access, list) and all(isinstance(a, str) for a in access):
            if any(a.lstrip('/') in {'*', 'carbon-intensity', 'carbon-intensity/latest'} for a in access):
                report['accessible_zones'].append(zone)
    for zone in zones:
        item = dict(zone=zone, zone_known=zone in data,
                    declared_latest_access=zone in report['accessible_zones'])
        if zone not in data:
            report['zones'].append(item | {'status': 'unknown_zone'})
            continue
        payload, status = await get('/carbon-intensity/latest', dict(zone=zone, temporalGranularity=temporal_granularity,
            emissionFactorType='lifecycle', flowTraced='true', disableCallerLookup='true'))
        item['status'] = status
        if status == 'ok':
            try:
                intensity = payload['carbonIntensity']
                observed = require_aware(datetime.fromisoformat(payload['datetime'].replace('Z', '+00:00')))
                if (payload['zone'] != zone or type(intensity) not in (int, float)
                        or not math.isfinite(intensity) or intensity < 0
                        or payload.get('emissionFactorType') != 'lifecycle'
                        or payload.get('flowTraced') is not True):
                    raise ValueError()
                item['returned_granularity'] = payload.get('temporalGranularity') if payload.get('temporalGranularity') in INTERVAL_SECONDS else None
                if payload.get('temporalGranularity') != temporal_granularity:
                    item['status'] = 'granularity_mismatch'
                    report['zones'].append(item)
                    continue
                age = (clock.now() - observed).total_seconds()
                item.update(status='future' if age < 0 else 'stale' if age > max_age_seconds else 'fresh',
                            observed_at=observed.isoformat(), age_seconds=age,
                            intensity_gco2e_kwh=float(intensity))
            except (ValueError, KeyError, TypeError, AttributeError):
                item['status'] = 'invalid_response'
        report['zones'].append(item)
        if status in {'authentication_failed', 'rate_limited'}:
            # Do not spend more requests after authentication or quota rejection.
            break
    report['status'] = ('ok' if all(z['status'] == 'fresh' for z in report['zones'])
                        and len(report['zones']) == len(zones) else 'attention_required')
    if not zones and not report['accessible_zones']:
        report['status'] = 'no_declared_carbon_access'
    return report
