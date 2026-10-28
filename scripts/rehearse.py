"""Install from lockfiles in a fresh source copy and rehearse the documented local setup."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from verification.harness import host_info,write_report


def source_copy(destination):
    ignore=shutil.ignore_patterns('node_modules','dist','__pycache__','*.pyc','*.tsbuildinfo','test-results','playwright-report')
    for name in ('backend','migrations','fixtures','frontend','scripts','verification','tests','guides','examples'):
        if (ROOT/name).exists():shutil.copytree(ROOT/name,destination/name,ignore=ignore)
    for name in ('pyproject.toml','requirements.lock','alembic.ini','.env.example','README.md'):
        shutil.copy2(ROOT/name,destination/name)


def main(output):
    report=dict(schema_version=1,profile='clean-environment-rehearsal',started_at=datetime.now(timezone.utc).isoformat(),
                host=host_info(),python_lock_sha256=hashlib.sha256((ROOT/'requirements.lock').read_bytes()).hexdigest(),
                node_lock_sha256=hashlib.sha256((ROOT/'frontend/package-lock.json').read_bytes()).hexdigest(),steps=[])
    environment={k:v for k,v in os.environ.items() if not k.startswith('GREENROUTE_') and k not in {'PYTHONPATH','VIRTUAL_ENV'}}
    environment['PIP_DISABLE_PIP_VERSION_CHECK']='1'
    started=time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='greenroute-clean-rehearsal-') as temporary:
        checkout=Path(temporary)/'source';checkout.mkdir();source_copy(checkout)
        python=checkout/'.venv'/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
        def run(name,command,timeout=180):
            print(name,flush=True);at=time.perf_counter()
            result=subprocess.run(command,cwd=checkout,env=environment,capture_output=True,text=True,timeout=timeout)
            step=dict(name=name,status='passed' if result.returncode==0 else 'failed',seconds=round(time.perf_counter()-at,3))
            if name in {'python tests','frontend tests'}:step['summary']=result.stdout.strip().splitlines()[-5:]
            report['steps'].append(step)
            if result.returncode:
                # Only installation/test output in this sanitized disposable source tree.
                raise RuntimeError(name+' failed: '+(result.stdout+result.stderr)[-3000:])
        try:
            run('create fresh virtual environment',[sys.executable,'-m','venv',str(checkout/'.venv')])
            run('install pinned Python dependencies',[str(python),'-m','pip','install','-r','requirements.lock'])
            run('check Python dependency consistency',[str(python),'-m','pip','check'])
            run('install pinned frontend dependencies',['npm','ci','--prefix','frontend'])
            run('build dashboard',['npm','run','build','--prefix','frontend'])
            run('python tests',[str(python),'-m','pytest','-q'])
            run('frontend tests',['npm','run','test','--prefix','frontend'])
            run('migrate/start/SDK/job/restart/dashboard smoke',[str(python),'scripts/run_verification.py','--smoke-only','--output','verification/rehearsal-smoke.json'])
            smoke=json.loads((checkout/'verification/rehearsal-smoke.json').read_text())
            assert smoke['status']=='passed'
            report['smoke']=smoke['smoke'];report['status']='passed'
            report['node']=subprocess.check_output(['node','--version'],text=True).strip()
            report['npm']=subprocess.check_output(['npm','--version'],text=True).strip()
        except Exception as exc:
            report.update(status='failed',error_type=type(exc).__name__,error=str(exc))
    report['elapsed_seconds']=round(time.perf_counter()-started,3)
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    write_report(output,report)
    print(f"Rehearsal {report['status']}; report: {output}",flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'verification'/'rehearsal-report.json')
    raise SystemExit(main(parser.parse_args().output))
