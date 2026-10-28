"""Plan a bounded chat trial; --execute is required to contact a gateway."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import httpx
from pydantic import ValidationError
from greenroute.clock import SystemClock
from greenroute.trial import TrialPlan, run_trial
from greenroute.batch_trial import BatchTrialPlan, run_batch_trial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--mode', choices=['demo', 'live'], default='demo')
    parser.add_argument('--model', default='demo-model')
    parser.add_argument('--requests', type=int, default=3)
    parser.add_argument('--max-tokens', type=int, default=32)
    parser.add_argument('--completion-token-budget', type=int)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    parser.add_argument('--kind', choices=['chat', 'batch'], default='chat')
    parser.add_argument('--trial-id', help='Required stable identifier for batch submissions')
    parser.add_argument('--allowed-region', action='append', default=[])
    parser.add_argument('--max-carbon', type=float)
    parser.add_argument('--wait-seconds', type=int, default=60)
    parser.add_argument('--max-polls', type=int, default=120)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    values = vars(args).copy()
    execute = values.pop('execute')
    kind = values.pop('kind')
    if values['completion_token_budget'] is None:
        values['completion_token_budget'] = 288 if kind == 'batch' else 96
    if kind == 'chat':
        if values['trial_id'] or values['allowed_region'] or values['max_carbon'] is not None:
            parser.error('Batch options require --kind batch')
        for name in ('trial_id', 'allowed_region', 'max_carbon', 'wait_seconds', 'max_polls'):
            values.pop(name)
    else:
        values['allowed_regions'] = values.pop('allowed_region')
    try:
        plan = BatchTrialPlan(**values) if kind == 'batch' else TrialPlan(**values)
    except ValidationError:
        print(json.dumps({'status': 'invalid_plan', 'network_requests': 0}))
        return 1
    if not execute:
        print(json.dumps({'status': 'dry_run', 'network_requests': 0, 'plan': plan.summary()}, indent=2))
        return 0
    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            runner = run_batch_trial if kind == 'batch' else run_trial
            return await runner(plan, client, os.environ.get('GREENROUTE_GATEWAY_KEY', ''), SystemClock())
    report = asyncio.run(run())
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
