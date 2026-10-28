"""Read-only Electricity Maps discovery and optional per-zone freshness checks."""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import httpx
from greenroute.clock import SystemClock
from greenroute.grid_access import check_access

KEY_NAME = 'GREENROUTE_ELECTRICITYMAPS_API_KEY'


def credential(env_file=None):
    value = os.environ.get(KEY_NAME, '')
    if value.strip() or env_file is None:
        return value
    # Read only the named assignment; never execute/source the file or expand shell code.
    for line in env_file.read_text().splitlines():
        match = re.fullmatch(r'\s*(?:export\s+)?' + KEY_NAME + r'\s*=\s*(.*?)\s*', line)
        if match:
            value = match[1]
            if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
                return value[1:-1]
            return value.split(' #', 1)[0].strip()
    return ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Read only the grid key assignment; environment takes precedence')
    parser.add_argument('--zone', action='append', default=[], help='Explicit Electricity Maps zone; repeat up to 10 times')
    parser.add_argument('--timeout-seconds', type=float, default=10)
    parser.add_argument('--max-age-seconds', type=float, default=300)
    args = parser.parse_args()
    if (len(args.zone) > 10 or len(set(args.zone)) != len(args.zone)
            or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', z) for z in args.zone)
            or not 0 < args.timeout_seconds <= 60 or not 0 < args.max_age_seconds <= 86400):
        parser.error('Use up to 10 unique zone IDs, timeout (0,60] and max age (0,86400]')
    try:
        key = credential(args.env_file)
    except (OSError, UnicodeError):
        print(json.dumps({'status': 'credential_file_unavailable', 'network_requests': 0}))
        return 1
    if not key.strip():
        print(json.dumps({'status': 'missing_credential', 'network_requests': 0}))
        return 1

    async def run():
        async with httpx.AsyncClient(trust_env=False, timeout=args.timeout_seconds) as client:
            return await check_access(client, key, SystemClock(), args.zone,
                                      args.timeout_seconds, args.max_age_seconds)
    try:
        report = asyncio.run(run())
    except Exception:
        # Never expose HTTPX tracebacks/request headers or raw provider error text.
        print(json.dumps({'status': 'check_failed'}))
        return 1
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
