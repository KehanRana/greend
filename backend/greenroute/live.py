"""Offline-tested live adapter boundary. Separate from the simulated runtime."""
import asyncio
import math
import os
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from greenroute.clock import require_aware
from greenroute.grid_policy import INTERVAL_SECONDS
from greenroute.grid import GridAdapterError, Observation
from greenroute.inference import execute_http


class Deployment(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    base_url: str
    api_key_env: str = Field(pattern=r'^[A-Z][A-Z0-9_]+$')
    region: str = Field(min_length=1)
    grid_zone: str = Field(min_length=1)
    model_alias: str = Field(min_length=1)
    served_model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    locality_evidence: str = Field(min_length=1)
    locality_verified: bool = False
    model_equivalence_verified: bool = False

    @field_validator('base_url')
    @classmethod
    def https_origin(cls, value):
        u = urlsplit(value)
        if (u.scheme != 'https' or not u.hostname or u.username or u.password
                or u.query or u.fragment or u.path.rstrip('/') != '/v1'):
            raise ValueError('A credential-free HTTPS URL ending in /v1 is required')
        return value.rstrip('/')

    def credential(self):
        value = os.environ.get(self.api_key_env, '')
        if not value.strip():
            raise ValueError('Deployment credential is unavailable')
        return value


class LiveManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    baseline_endpoint: str
    deployments: list[Deployment] = Field(min_length=2)

    @model_validator(mode='after')
    def equivalent_deployments(self):
        ids = [d.id for d in self.deployments]
        if len(set(ids)) != len(ids) or self.baseline_endpoint not in ids:
            raise ValueError('Unique deployments and an explicit registered baseline are required')
        if len({(d.model_alias, d.model_version) for d in self.deployments}) != 1:
            raise ValueError('Deployments must declare the same model alias and version')
        return self


class VLLMAdapter:
    def __init__(self, client: httpx.AsyncClient, timeout_seconds=60):
        if not 0 < timeout_seconds <= 300:
            raise ValueError('Timeout must be between zero and 300 seconds')
        self.client, self.timeout_seconds = client, timeout_seconds

    async def probe(self, deployment: Deployment):
        """Read-only model discovery. Does not establish processing locality or equivalence."""
        key = deployment.credential()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self.client.get(deployment.base_url + '/models',
                    headers={'authorization': f'Bearer {key}'}, follow_redirects=False)
                response.raise_for_status()
                data = response.json()['data']
                if not isinstance(data, list) or not any(
                    isinstance(m, dict) and m.get('id') == deployment.served_model for m in data):
                    raise ValueError()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise ValueError('Regional model probe failed') from None
        return {'endpoint_id': deployment.id, 'model_available': True,
                'locality_verified': False}

    async def execute(self, deployment: Deployment, payload, request_id):
        if payload.model != deployment.model_alias:
            raise ValueError('Request model does not match deployment alias')
        body = payload.model_dump(exclude_none=True)
        body['model'] = deployment.served_model
        return await execute_http(self.client, deployment.base_url + '/chat/completions',
                                  deployment.credential(), body, request_id, self.timeout_seconds)


class ElectricityMapsAdapter:
    """v4 lifecycle, consumption-based signals. No fixture fallback and no retries here."""
    def __init__(self, client: httpx.AsyncClient, api_key: SecretStr, clock,
                 timeout_seconds=5, max_age_seconds=300, temporal_granularity="hourly"):
        if temporal_granularity not in INTERVAL_SECONDS:
            raise ValueError("Unsupported grid granularity")
        self.temporal_granularity = temporal_granularity
        if not api_key.get_secret_value().strip() or not 0 < timeout_seconds <= 60 or max_age_seconds <= 0:
            raise ValueError('A grid credential and positive bounded timeout/freshness are required')
        self.client, self.api_key, self.clock = client, api_key, clock
        self.timeout_seconds, self.max_age_seconds = timeout_seconds, max_age_seconds

    async def fetch(self, zones: list[str]) -> list[Observation]:
        if len(zones) != len(set(zones)) or any(not z.strip() for z in zones):
            raise GridAdapterError('Unique explicit grid zones are required')
        observations = []
        try:
            async with asyncio.timeout(self.timeout_seconds):
                for zone in zones:
                    response = await self.client.get(
                        'https://api.electricitymaps.com/v4/carbon-intensity/latest',
                        params={'zone': zone, 'temporalGranularity': self.temporal_granularity,
                                'emissionFactorType': 'lifecycle',
                                'flowTraced': 'true', 'disableCallerLookup': 'true'},
                        headers={'auth-token': self.api_key.get_secret_value()}, follow_redirects=False)
                    response.raise_for_status()
                    data = response.json()
                    intensity = data['carbonIntensity']
                    observed = require_aware(datetime.fromisoformat(data['datetime'].replace('Z', '+00:00')))
                    age = (self.clock.now() - observed).total_seconds()
                    if (data['zone'] != zone or type(intensity) not in (float, int)
                            or not math.isfinite(intensity) or intensity < 0
                            or not 0 <= age <= self.max_age_seconds
                            or data.get('emissionFactorType') != 'lifecycle'
                            or data.get('flowTraced') is not True
                            or data.get('temporalGranularity') != self.temporal_granularity):
                        raise ValueError()
                    # Source timestamp is preserved; updatedAt is not the observation time.
                    observations.append(Observation(zone, float(intensity), observed,
                        'electricitymaps-v4:lifecycle:consumption', mode='live',
                        temporal_granularity=self.temporal_granularity))
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            raise GridAdapterError('Live grid batch unavailable or invalid') from None
        return observations
