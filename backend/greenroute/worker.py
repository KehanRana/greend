"""Independent durable scheduler. Run with python -m greenroute.worker."""
import asyncio
import json
import logging
import signal
from contextlib import suppress
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx
from sqlalchemy import text

from greenroute.clock import SystemClock
from greenroute.config import Settings
from greenroute.contracts import ChatRequest, JobPolicy
from greenroute.endpoint_health import record_outcome
from greenroute.estimation import capture_context, persist
from greenroute.grid import latest_snapshots
from greenroute.inference import execute, UpstreamFailure
from greenroute.jobs import cleanup, stamp, terminal_values
from greenroute.policy import carbon_decision
from greenroute.store import immediate, make_engine, ready
from greenroute.runtime import check_runtime, policy_options, execute_endpoint
from greenroute.live import VLLMAdapter

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, engine, settings, client, clock=None):
        self.engine,self.settings,self.client = engine,settings,client
        check_runtime(engine, settings)
        self.clock = clock or SystemClock()
        self.tasks = set()

    def recover(self):
        now = self.clock.now()
        with immediate(self.engine) as conn:
            conn.execute(text("UPDATE attempts SET outcome='unknown',uncertain=1,ended_at=:at "
                "WHERE job_id IS NULL AND outcome='running' AND started_at<:cutoff"),
                dict(at=stamp(now),cutoff=stamp(now-timedelta(seconds=self.settings.timeout_seconds+self.settings.lease_seconds))))
            expired = list(conn.execute(text("SELECT id,last_decision_id FROM jobs WHERE state='running' AND lease_expires_at<=:at"),
                                        dict(at=stamp(now))).mappings())
            for job in expired:
                conn.execute(text("UPDATE attempts SET outcome='unknown',uncertain=1,ended_at=:at "
                                  "WHERE job_id=:id AND outcome='running'"),dict(id=job['id'],at=stamp(now)))
                conn.execute(text("UPDATE jobs SET state='unknown',error=:error,lease_owner=NULL,lease_expires_at=NULL,"
                    "terminal_at=:terminal,cleanup_at=:cleanup,metadata_expires_at=:metadata WHERE id=:id"),
                    dict(id=job['id'],error=json.dumps({'code':'lease_expired','message':'Provider completion could not be established'}),
                         **terminal_values(now)))

    def claim(self, jid):
        check_runtime(self.engine, self.settings)
        with immediate(self.engine) as conn:
            now = self.clock.now()
            job = conn.execute(text("SELECT * FROM jobs WHERE id=:id AND state IN ('queued','waiting_for_carbon','retry_pending')"),
                               dict(id=jid)).mappings().first()
            if not job:
                return None
            deadline = datetime.fromisoformat(job['deadline_at'])
            if job['next_attempt_at'] and datetime.fromisoformat(job['next_attempt_at']) > now:
                if now >= deadline:
                    conn.execute(text("UPDATE jobs SET state='expired',terminal_at=:terminal,cleanup_at=:cleanup,"
                        "metadata_expires_at=:metadata WHERE id=:id"),dict(id=jid,**terminal_values(now)))
                return None
            endpoints = list(conn.execute(text('SELECT * FROM endpoints')).mappings())
            policy = JobPolicy.model_validate_json(job['policy'])
            request = ChatRequest.model_validate_json(job['payload'])
            decision_id = 'req_'+uuid4().hex
            snapshots=latest_snapshots(self.engine, self.settings.mode)
            d = carbon_decision(decision_id,request.model,endpoints,policy,snapshots,now,
                                self.settings.grid_max_age_seconds, **policy_options(self.settings))
            if d.action == 'select':
                d.estimation_context=capture_context(self.settings,request.model,endpoints,d.eligible,snapshots,now)
                running=conn.execute(text("SELECT COUNT(*) FROM jobs WHERE state='running'")).scalar()
                if running >= self.settings.worker_concurrency:
                    return None
            # Keep a single waiting decision until its reasoning changes. Dispatch
            # always writes a fresh immutable decision with its exact snapshot IDs.
            old = conn.execute(text('SELECT data FROM decisions WHERE request_id=:id'),dict(id=job['last_decision_id'])).scalar()
            unchanged = False
            if old and d.action == 'wait':
                prior = json.loads(old)
                fields = ('action','eligible','excluded','signal_statuses','reason','exceptions')
                unchanged = all(prior.get(k) == getattr(d,k) for k in fields)
            if not unchanged:
                conn.execute(text('INSERT INTO decisions (request_id,created_at,data,job_id) VALUES (:id,:at,:data,:job)'),
                             dict(id=decision_id,at=stamp(now),data=d.model_dump_json(),job=jid))
                conn.execute(text('UPDATE jobs SET last_decision_id=:decision WHERE id=:id'),dict(id=jid,decision=decision_id))
            if d.action == 'wait':
                conn.execute(text("UPDATE jobs SET state='waiting_for_carbon' WHERE id=:id"),dict(id=jid))
                return None
            if d.action == 'expire':
                conn.execute(text("UPDATE jobs SET state='expired',terminal_at=:terminal,cleanup_at=:cleanup,"
                    "metadata_expires_at=:metadata WHERE id=:id"),dict(id=jid,**terminal_values(now)))
                return None
            if job['attempt_count'] >= 3:
                conn.execute(text("UPDATE jobs SET state='failed',terminal_at=:terminal,cleanup_at=:cleanup,"
                    "metadata_expires_at=:metadata WHERE id=:id"),dict(id=jid,**terminal_values(now)))
                return None
            owner, attempt_id = uuid4().hex,'attempt_'+uuid4().hex
            lease = stamp(now+timedelta(seconds=self.settings.lease_seconds))
            conn.execute(text("UPDATE jobs SET state='running',lease_owner=:owner,lease_expires_at=:lease,"
                              "attempt_count=attempt_count+1,next_attempt_at=NULL WHERE id=:id"),dict(id=jid,owner=owner,lease=lease))
            conn.execute(text("INSERT INTO attempts (id,request_id,endpoint_id,started_at,outcome,uncertain,job_id) "
                              "VALUES (:id,:request,:endpoint,:at,'running',0,:job)"),
                         dict(id=attempt_id,request=decision_id,endpoint=d.selected_endpoint,at=stamp(now),job=jid))
            endpoint = next(e for e in endpoints if e['id'] == d.selected_endpoint)
            return dict(id=jid,owner=owner,attempt=attempt_id,request_id=decision_id,endpoint=endpoint['id'],
                        region=endpoint['region'],payload=request,count=job['attempt_count']+1,deadline=deadline)

    def renew(self, claim):
        now = self.clock.now()
        with immediate(self.engine) as conn:
            return conn.execute(text("UPDATE jobs SET lease_expires_at=:lease WHERE id=:id AND state='running' "
                                     "AND lease_owner=:owner AND lease_expires_at>:at"),
                dict(id=claim['id'],owner=claim['owner'],at=stamp(now),
                     lease=stamp(now+timedelta(seconds=self.settings.lease_seconds)))).rowcount == 1

    async def heartbeat(self, claim):
        while True:
            await asyncio.sleep(self.settings.lease_seconds/3)
            if not self.renew(claim):
                return  # Lost ownership: caller cancels the provider call.

    def finish(self, claim, result=None, failure=None):
        now = self.clock.now()
        with immediate(self.engine) as conn:
            owned = conn.execute(text("SELECT id FROM jobs WHERE id=:id AND state='running' AND lease_owner=:owner "
                                      "AND lease_expires_at>:at"),dict(id=claim['id'],owner=claim['owner'],at=stamp(now))).first()
            if not owned:
                return False
            error, retry_at = None,None
            state = 'succeeded'
            if failure:
                error = json.dumps({'code':failure.code,'message':failure.message})
                state = 'unknown' if failure.uncertain else 'failed'
                delay = self.settings.retry_base_seconds * 2 ** (claim['count']-1)
                if failure.retry_after:
                    try:
                        guidance = float(failure.retry_after) if failure.retry_after.isdigit() else (
                            parsedate_to_datetime(failure.retry_after)-now).total_seconds()
                        delay = max(delay,guidance)
                    except (ValueError,TypeError,OverflowError):
                        pass
                if failure.safe_to_retry and claim['count'] < 3 and delay < (claim['deadline']-now).total_seconds():
                    state,retry_at = 'retry_pending',stamp(now+timedelta(seconds=delay))
            conn.execute(text('UPDATE attempts SET ended_at=:at,outcome=:outcome,uncertain=:uncertain,usage=:usage,'
                              'upstream_request_id=:upstream WHERE id=:id'),
                dict(id=claim['attempt'],at=stamp(now),outcome='succeeded' if result else ('unknown' if failure.uncertain else 'failed'),
                     uncertain=bool(failure and failure.uncertain),usage=json.dumps(result.usage) if result else None,
                     upstream=result.upstream_request_id if result else None))
            terminal = terminal_values(now) if state != 'retry_pending' else dict(terminal=None,cleanup=None,metadata=None)
            conn.execute(text('UPDATE jobs SET state=:state,result=:result,error=:error,next_attempt_at=:retry,'
                              'lease_owner=NULL,lease_expires_at=NULL,terminal_at=:terminal,cleanup_at=:cleanup,'
                              'metadata_expires_at=:metadata WHERE id=:id'),
                dict(id=claim['id'],state=state,result=json.dumps(result.completion) if result else None,
                     error=error,retry=retry_at,**terminal))
            if result:
                decision=json.loads(conn.execute(text('SELECT data FROM decisions WHERE request_id=:id'),
                                    dict(id=claim['request_id'])).scalar_one())
                persist(conn,claim['attempt'],decision,result.usage,now)
        record_outcome(self.engine,claim['endpoint'],failure,now)
        return True

    async def execute_claim(self, claim):
        call = asyncio.create_task(execute_endpoint(self.client,self.settings,claim['endpoint'],claim['region'],claim['payload'],claim['request_id']))
        heartbeat = asyncio.create_task(self.heartbeat(claim))
        try:
            done,_ = await asyncio.wait({call,heartbeat},return_when=asyncio.FIRST_COMPLETED)
            if call not in done:
                # Heartbeat failed/lost ownership. Leave the durable running record
                # for conservative lease recovery; never commit a late completion.
                return
            try:
                self.finish(claim,result=call.result())
            except UpstreamFailure as exc:
                self.finish(claim,failure=exc)
        finally:
            for task in (call,heartbeat):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError,Exception):
                    await task

    async def probe_due(self):
        now=self.clock.now()
        with self.engine.connect() as conn:
            rows = list(conn.execute(text("SELECT * FROM endpoints WHERE healthy=0 AND disabled_reason='circuit_open' "
                                          "AND probe_after<=:at"),dict(at=stamp(now))).mappings())
        for endpoint in rows:
            success=False
            configuration_failure=False
            try:
                if self.settings.mode == 'live':
                    manifest = check_runtime(self.engine, self.settings)
                    deployment = next(d for d in manifest.deployments if d.id == endpoint['id'])
                    await VLLMAdapter(self.client,self.settings.timeout_seconds).probe(deployment)
                    success = True
                else:
                    async with asyncio.timeout(self.settings.timeout_seconds):
                        response=await self.client.get(f"{self.settings.upstream_url}/{endpoint['region']}/health",
                            headers={'authorization':f'Bearer {self.settings.upstream_key.get_secret_value()}'},
                            follow_redirects=False)
                    success=response.status_code == 200
                    configuration_failure=response.status_code in {401,403,404}
            except (httpx.RequestError,TimeoutError,ValueError):
                pass
            with immediate(self.engine) as conn:
                conn.execute(text("UPDATE endpoints SET healthy=:healthy,consecutive_failures=:count,"
                    "disabled_reason=:reason,probe_after=:next WHERE id=:id AND disabled_reason='circuit_open'"),
                    dict(id=endpoint['id'],healthy=success,count=0 if success else endpoint['consecutive_failures'],
                         reason=None if success else ('configuration_error' if configuration_failure else 'circuit_open'),
                         next=None if success or configuration_failure else stamp(now+timedelta(seconds=30))))

    async def tick(self):
        check_runtime(self.engine, self.settings)
        for task in list(self.tasks):
            if task.done():
                self.tasks.remove(task)
                if not task.cancelled() and task.exception():
                    logger.error('Job execution interrupted; lease recovery will preserve uncertainty')
        self.recover()
        cleanup(self.engine,self.clock.now())
        await self.probe_due()
        with self.engine.connect() as conn:
            ids=list(conn.execute(text("SELECT id FROM jobs WHERE state IN ('queued','waiting_for_carbon','retry_pending') "
                "ORDER BY created_at,id LIMIT :limit"),dict(limit=self.settings.pending_job_limit)).scalars())
        for jid in ids:
            if len(self.tasks) >= self.settings.worker_concurrency:
                break
            claim=self.claim(jid)
            if claim:
                self.tasks.add(asyncio.create_task(self.execute_claim(claim)))

    async def drain(self):
        if self.tasks:
            results=await asyncio.gather(*self.tasks,return_exceptions=True)
            if any(isinstance(r,BaseException) for r in results):
                logger.error("Execution interrupted; pending leases will be recovered conservatively")
            self.tasks.clear()


async def main():
    settings=Settings.from_env()
    engine=make_engine(settings.database)
    check_runtime(engine,settings)
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        loop.add_signal_handler(sig,stop.set)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_seconds,connect=5),trust_env=False) as client:
            worker=Worker(engine,settings,client)
            stop_task=asyncio.create_task(stop.wait())
            try:
                while not stop.is_set():
                    try:
                        await worker.tick()
                    except Exception:
                        logger.error('Worker cycle failed; durable jobs will be reevaluated')
                    await asyncio.wait({stop_task,*worker.tasks},timeout=settings.worker_poll_seconds,
                                       return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_task
                await worker.drain()
    finally:
        engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
