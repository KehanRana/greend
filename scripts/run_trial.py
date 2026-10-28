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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--mode', choices=['demo', 'live'], default='demo')
    parser.add_argument('--model', default='demo-model')
    parser.add_argument('--requests', type=int, default=3)
    parser.add_argument('--max-tokens', type=int, default=32)
    parser.add_argument('--completion-token-budget', type=int, default=96)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    execute = vars(args).pop('execute')
    try:
        plan = TrialPlan(**vars(args))
    except ValidationError:
        print(json.dumps({'status': 'invalid_plan', 'network_requests': 0}))
        return 1
    if not execute:
        print(json.dumps({'status': 'dry_run', 'network_requests': 0, 'plan': plan.summary()}, indent=2))
        return 0
    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            return await run_trial(plan, client, os.environ.get('GREENROUTE_GATEWAY_KEY', ''), SystemClock())
    report = asyncio.run(run())
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
