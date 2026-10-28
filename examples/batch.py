"""Submit and poll one job. Start the mock, gateway and worker first."""
import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8000',help='Gateway origin')
    parser.add_argument('--max-carbon',type=float,default=None,
                        help='Optional gCO2e/kWh threshold; 150 demonstrates waiting for the fixture drop')
    args=parser.parse_args()
    payload={'request':{'model':'demo-model','messages':[{'role':'user','content':'Summarize regional grid-aware inference.'}]},
             'policy':{'allowed_regions':['us-east-1','eu-central-1','ap-southeast-2'],
                       'max_carbon_gco2e_kwh':args.max_carbon,
                       'deadline_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
                       'deadline_policy':'expire'}}
    headers={'authorization':f"Bearer {os.environ['GREENROUTE_GATEWAY_KEY']}", 'idempotency-key':str(uuid4())}
    with httpx.Client(base_url=args.base_url,headers=headers,timeout=60,trust_env=False) as client:
        response=client.post('/api/jobs',json=payload)
        response.raise_for_status()
        job=response.json()
        print('Job:',job['id'],flush=True)
        stop_at=time.monotonic()+390
        last_status=None
        while time.monotonic()<stop_at:
            response=client.get(job['status_url'])
            response.raise_for_status()
            detail=response.json()
            if detail['status'] != last_status:
                print('Status:',detail['status'],flush=True)
                last_status=detail['status']
            if detail['status'] == 'succeeded':
                print(detail['result']['choices'][0]['message']['content'])
                return
            if detail['status'] in {'failed','expired','cancelled','unknown'}:
                raise SystemExit(f"Job ended: {detail['status']}; inspect {job['status_url']}")
            time.sleep(1)
        raise SystemExit(f"Polling stopped; job remains available at {job['status_url']}")


if __name__ == '__main__':
    main()
