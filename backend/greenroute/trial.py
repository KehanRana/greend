"""Small, sequential chat trials with explicit dispatch and completion-token limits."""
import time
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import httpx

PROMPT = 'In one sentence, explain why flexible inference can follow cleaner electricity.'


class TrialPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    base_url: str = 'http://127.0.0.1:8000'
    mode: str = 'demo'
    model: str = Field(default='demo-model', min_length=1, max_length=128)
    requests: int = Field(default=3, ge=1, le=20)
    max_tokens: int = Field(default=32, ge=1, le=256)
    completion_token_budget: int = Field(default=96, ge=1, le=5120)
    timeout_seconds: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)

    @field_validator('mode')
    @classmethod
    def mode_known(cls, value):
        if value not in {'demo', 'live'}:
            raise ValueError('Mode must be demo or live')
        return value

    @field_validator('base_url')
    @classmethod
    def origin_only(cls, value):
        url = urlsplit(value)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path not in {'', '/'} or not (url.scheme == 'https' or
                    url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'})):
            raise ValueError('Use an HTTPS gateway origin or loopback HTTP origin')
        return value.rstrip('/')

    @model_validator(mode='after')
    def budget_sufficient(self):
        if self.requests * self.max_tokens > self.completion_token_budget:
            raise ValueError('Requested completion caps exceed the trial budget')
        return self

    def summary(self):
        return dict(mode=self.mode, model=self.model, max_dispatches=self.requests,
                    max_tokens_per_request=self.max_tokens,
                    reserved_completion_tokens=self.requests * self.max_tokens,
                    completion_token_budget=self.completion_token_budget,
                    prompt=PROMPT, automatic_retries=False,
                    budget_scope='completion tokens only; input tokens and monetary charges are not capped')


def safe_usage(data):
    usage = data.get('usage') if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys):
        return None
    if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
        return None
    return {k: usage[k] for k in keys}


def safe_id(value):
    return value if isinstance(value, str) and len(value) <= 256 else None


async def run_trial(plan, client, key, clock):
    report = dict(status='preflight_failed', started_at=clock.now().isoformat(), plan=plan.summary(),
                  network_requests=0, dispatches=0, executions=[], energy_is_illustrative=True)
    headers = {'authorization': f'Bearer {key}'}
    if not key.strip():
        return report | {'reason': 'missing_gateway_credential'}

    async def request(method, path, **kwargs):
        report['network_requests'] += 1
        return await client.request(method, plan.base_url + path, headers=headers,
                                    follow_redirects=False, timeout=plan.timeout_seconds, **kwargs)

    try:
        ready = await request('GET', '/ready')
        if ready.status_code != 200 or ready.json().get('mode') != plan.mode:
            return report | {'reason': 'gateway_not_ready_or_mode_mismatch'}
        regions = await request('GET', '/api/regions')
        metrics = await request('GET', '/api/metrics')
        if regions.status_code != 200 or metrics.status_code != 200:
            return report | {'reason': 'metadata_unavailable'}
        metadata, metric_data = regions.json(), metrics.json()
        if metadata['mode'] != plan.mode or metric_data['mode'] != plan.mode:
            return report | {'reason': 'metadata_mode_mismatch'}
        baseline = metric_data['configuration']['baseline_endpoint']
        eligible = [r for r in metadata['items'] if r['model'] == plan.model and r['healthy']
                    and r['locality'] == ('verified' if plan.mode == 'live' else 'simulated')]
        eligible.sort(key=lambda r: (r['latency_rank'], r['endpoint_id']))
        if not eligible or baseline not in {r['endpoint_id'] for r in eligible}:
            return report | {'reason': 'model_or_baseline_ineligible'}
        required = {baseline, eligible[0]['endpoint_id']}
        if any(r['signal_status'] != 'fresh' for r in eligible if r['endpoint_id'] in required):
            return report | {'reason': 'fresh_comparison_signals_required'}
        report['grid_policy'] = metadata.get('grid_policy')
        report['baseline_endpoint'] = baseline
        report['signals_at_preflight'] = [{k: r.get(k) for k in (
            'endpoint_id', 'grid_zone', 'signal_status', 'snapshot_id', 'source',
            'observed_at', 'retrieved_at', 'intensity_gco2e_kwh', 'temporal_granularity')} for r in eligible]
        report['status'] = 'running'
        for _ in range(plan.requests):
            report['dispatches'] += 1
            started = time.monotonic()
            try:
                response = await request('POST', '/v1/chat/completions', json=dict(model=plan.model,
                    messages=[dict(role='user', content=PROMPT)], max_tokens=plan.max_tokens, stream=False))
            except (httpx.RequestError, TimeoutError):
                report['executions'].append({'status': 'unknown', 'reason': 'transport_interrupted'})
                report['status'] = 'stopped'
                break
            item = dict(http_status=response.status_code, latency_ms=round((time.monotonic()-started)*1000, 3),
                request_id=safe_id(response.headers.get('x-greenroute-request-id')),
                region=safe_id(response.headers.get('x-greenroute-region')),
                mode=safe_id(response.headers.get('x-greenroute-mode')))
            report['executions'].append(item)
            if response.status_code != 200:
                item['status'] = 'unknown' if response.status_code >= 500 else 'rejected'
                report['status'] = 'stopped'
                break
            data = response.json()
            item.update(provider_completion_id=safe_id(data.get('id')), usage=safe_usage(data))
            if item['mode'] != plan.mode or not item['request_id'] or not item['provider_completion_id']:
                item.update(status='unknown', reason='missing_or_mismatched_provenance')
                report['status'] = 'stopped'
                break
            if item['usage'] is None or item['usage']['completion_tokens'] > plan.max_tokens:
                item.update(status='unknown', reason='invalid_usage_or_completion_cap_exceeded')
                report['status'] = 'stopped'
                break
            item['status'] = 'succeeded'
        else:
            report['status'] = 'completed'
    except (httpx.RequestError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        report.update(status='stopped' if report['dispatches'] else 'preflight_failed', reason='invalid_or_unavailable_response')
        if report['dispatches'] and report['executions']:
            report['executions'][-1]['status'] = 'unknown'
    report['finished_at'] = clock.now().isoformat()
    report['reported_completion_tokens'] = sum(e.get('usage', {}).get('completion_tokens', 0)
        for e in report['executions'] if e.get('usage'))
    report['note'] = 'No retries or batch submissions. Preflight signals are not per-dispatch carbon evidence; inspect gateway activity for estimates.'
    return report
