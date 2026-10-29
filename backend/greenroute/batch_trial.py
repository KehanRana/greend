"""Bounded sequential job trials; reserve capacity for all three worker attempts."""
import asyncio
import time
from datetime import timedelta
from pydantic import Field, field_validator, model_validator
import httpx
from greenroute.trial import TrialPlan, PROMPT, safe_id, safe_usage

from greenroute.jobs import MAX_JOB_ATTEMPTS

WORKER_ATTEMPT_LIMIT = MAX_JOB_ATTEMPTS


class BatchTrialPlan(TrialPlan):
    allowed_regions: list[str] = Field(min_length=1, max_length=20)
    trial_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    completion_token_budget: int = Field(default=288, ge=1, le=15360)
    max_carbon: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wait_seconds: int = Field(default=60, ge=1, le=300)
    poll_seconds: float = Field(default=1, ge=0.1, le=10, allow_inf_nan=False)
    max_polls: int = Field(default=120, ge=1, le=1000)

    @field_validator('allowed_regions')
    @classmethod
    def explicit_regions(cls, values):
        if len(set(values)) != len(values) or any(not v.strip() or len(v) > 128 for v in values):
            raise ValueError('Unique explicit region names are required')
        return values

    @model_validator(mode='after')
    def reserve_attempts(self):
        if self.requests * self.max_tokens * WORKER_ATTEMPT_LIMIT > self.completion_token_budget:
            raise ValueError('Budget must cover all three possible worker attempts per job')
        return self

    def summary(self):
        return super().summary() | dict(kind='batch', trial_id=self.trial_id,
            allowed_regions=self.allowed_regions, max_carbon=self.max_carbon,
            max_jobs=self.requests, max_dispatches=self.requests * WORKER_ATTEMPT_LIMIT,
            reserved_completion_tokens=self.requests * self.max_tokens * WORKER_ATTEMPT_LIMIT,
            worker_attempt_limit=WORKER_ATTEMPT_LIMIT, automatic_retries=False,
            worker_safe_retries_possible=True, deadline_policy='expire', wait_seconds=self.wait_seconds,
            max_polls=self.max_polls, poll_seconds=self.poll_seconds)


async def run_batch_trial(plan, client, key, clock, sleep=asyncio.sleep, monotonic=time.monotonic):
    report = dict(status='preflight_failed', plan=plan.summary(), started_at=clock.now().isoformat(),
                  network_requests=0, submissions=0, polls=0, jobs=[], energy_is_illustrative=True)
    if not key.strip():
        return report | {'reason': 'missing_gateway_credential'}

    async def request(method, path, **kwargs):
        report['network_requests'] += 1
        return await client.request(method, plan.base_url + path,
            headers={'authorization': f'Bearer {key}'} | kwargs.pop('headers', {}),
            follow_redirects=False, timeout=plan.timeout_seconds, **kwargs)

    try:
        ready = await request('GET', '/ready')
        if ready.status_code != 200 or ready.json().get('mode') != plan.mode:
            return report | {'reason': 'gateway_not_ready_or_mode_mismatch'}
        response = await request('GET', '/api/regions')
        if response.status_code != 200 or response.json().get('mode') != plan.mode:
            return report | {'reason': 'regional_metadata_unavailable'}
        report['grid_policy'] = response.json().get('grid_policy')
        eligible = [r for r in response.json()['items'] if r['healthy'] and r['model'] == plan.model
            and r['region'] in plan.allowed_regions
            and r['locality'] == ('verified' if plan.mode == 'live' else 'simulated')]
        if not eligible:
            return report | {'reason': 'no_eligible_endpoint'}
        # Stale/dirty signals are intentional batch waiting scenarios, not a preflight failure.
        report['status'] = 'running'
        for index in range(plan.requests):
            item = dict(idempotency_key=f'trial-{plan.trial_id}-{index}', status='submission_unknown', transitions=[])
            report['jobs'].append(item)
            deadline = clock.now() + timedelta(seconds=plan.wait_seconds)
            stop_at = monotonic() + plan.wait_seconds
            report['submissions'] += 1
            response = await request('POST', '/api/jobs', headers={'idempotency-key': item['idempotency_key']},
                json=dict(request=dict(model=plan.model, messages=[dict(role='user', content=PROMPT)],
                                       max_tokens=plan.max_tokens, stream=False),
                    policy=dict(allowed_regions=plan.allowed_regions, max_carbon_gco2e_kwh=plan.max_carbon,
                                deadline_at=deadline.isoformat(), deadline_policy='expire')))
            if response.status_code != 202:
                item.update(status='submission_rejected' if response.status_code < 500 else 'submission_unknown',
                            http_status=response.status_code)
                report['status'] = 'stopped'
                break
            jid = response.json()['id']
            if not isinstance(jid, str) or not jid.startswith('job_') or not jid[4:].isalnum() or len(jid) > 128:
                raise ValueError('Invalid job identity')
            item['job_id'] = jid
            while report['polls'] < plan.max_polls and monotonic() < stop_at:
                report['polls'] += 1
                response = await request('GET', '/api/jobs/' + jid)
                if response.status_code != 200:
                    item.update(status='polling_stopped', http_status=response.status_code)
                    break
                detail = response.json()
                if detail['mode'] != plan.mode or detail['id'] != jid:
                    item.update(status='polling_stopped', reason='job_identity_or_mode_mismatch')
                    break
                state = detail['status']
                if not item['transitions'] or item['transitions'][-1]['state'] != state:
                    item['transitions'].append(dict(state=state, observed_at=clock.now().isoformat()))
                if state in {'succeeded', 'failed', 'expired', 'cancelled', 'unknown'}:
                    item.update(status=state, attempt_count=detail['attempt_count'])
                    attempts = detail['attempts']
                    if (type(detail['attempt_count']) is not int or detail['attempt_count'] > WORKER_ATTEMPT_LIMIT
                            or len(attempts) > WORKER_ATTEMPT_LIMIT):
                        raise ValueError('Attempt limit exceeded')
                    item['attempts'] = [{k: a.get(k) for k in ('id', 'request_id', 'endpoint_id', 'outcome', 'uncertain')}
                                        | {'usage': safe_usage({'usage': a.get('usage')})} for a in attempts]
                    decision = detail.get('decision') or {}
                    item['decision'] = {k: decision.get(k) for k in ('request_id', 'mode', 'selected_endpoint',
                        'reason', 'snapshot_ids', 'signal_statuses', 'evaluated_at', 'estimation_context')}
                    if state == 'succeeded':
                        usage = safe_usage(detail.get('result'))
                        item.update(usage=usage, provider_completion_id=safe_id((detail.get('result') or {}).get('id')))
                        if (not usage or usage['completion_tokens'] > plan.max_tokens
                                or not item['provider_completion_id'] or decision.get('mode') != plan.mode):
                            item.update(status='unknown', reason='invalid_usage_or_provenance')
                    break
                if state not in {'queued', 'waiting_for_carbon', 'running', 'retry_pending'}:
                    raise ValueError('Unknown job state')
                await sleep(min(plan.poll_seconds, max(0, stop_at-monotonic())))
            else:
                item['status'] = 'polling_stopped'
            if item['status'] != 'succeeded':
                report['status'] = 'stopped'
                break
        else:
            report['status'] = 'completed'
    except (httpx.RequestError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        report.update(status='stopped' if report['submissions'] else 'preflight_failed',
                      reason='invalid_or_unavailable_response')
        if report['jobs']:
            item = report['jobs'][-1]
            item['status'] = ('unknown' if item['status'] == 'succeeded' else
                              'polling_stopped' if item.get('job_id') else 'submission_unknown')
    report['finished_at'] = clock.now().isoformat()
    report['note'] = ('No submission retries or automatic cancellations. Polling stop does not stop a queued or running job. '
                      'Keep job IDs/idempotency keys; do not replace uncertain submissions with new keys. Energy is illustrative.')
    return report
