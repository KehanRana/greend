"""Durable job admission and owner-authorized retrieval; no upstream I/O."""
import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from greenroute.store import immediate

MAX_JOB_ATTEMPTS = 3

PENDING = ('queued','waiting_for_carbon','retry_pending')
TERMINAL = ('succeeded','failed','expired','cancelled','unknown')


def stamp(value):
    return value.isoformat(timespec='microseconds')


class JobError(Exception):
    def __init__(self,status,message,code):
        self.status,self.message,self.code = status,message,code


def terminal_values(now):
    return dict(terminal=stamp(now), cleanup=stamp(now+timedelta(hours=24)),
                metadata=stamp(now+timedelta(days=7)))


def submit(engine, owner, key, submission, now, capacity):
    normalized = submission.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps(normalized,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with immediate(engine) as conn:
        existing = conn.execute(text('SELECT * FROM jobs WHERE owner_id=:owner AND idempotency_key=:key'),
                                dict(owner=owner,key=key)).mappings().first()
        if existing and existing['idempotency_expires_at'] and datetime.fromisoformat(existing['idempotency_expires_at']) > now:
            if existing['body_digest'] != digest:
                raise JobError(409,'Idempotency key was used with a different request','idempotency_conflict')
            return existing['id'], existing['state']
        if existing:
            conn.execute(text('UPDATE jobs SET idempotency_key=:key,body_digest=\'\' WHERE id=:id'),
                         dict(key='retired_'+uuid4().hex,id=existing['id']))
        try:
            submission.policy.ensure_submittable(now)
        except ValueError:
            raise JobError(422,'Deadline must be future and within 24 hours','invalid_deadline') from None
        count = conn.execute(text("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','waiting_for_carbon','retry_pending')")).scalar()
        if count >= capacity:
            raise JobError(429,'Pending job capacity reached','queue_full')
        jid = 'job_'+uuid4().hex
        conn.execute(text('INSERT INTO jobs (id,owner_id,idempotency_key,body_digest,payload,policy,state,created_at,'
                          'deadline_at,idempotency_expires_at) VALUES (:id,:owner,:key,:digest,:payload,:policy,\'queued\',:at,:deadline,:expires)'),
            dict(id=jid,owner=owner,key=key,digest=digest,payload=json.dumps(normalized['request']),
                 policy=json.dumps(normalized['policy']),at=stamp(now),deadline=stamp(submission.policy.deadline_at),
                 expires=stamp(now+timedelta(hours=24))))
    return jid,'queued'


def cancel(engine, owner, jid, now):
    with immediate(engine) as conn:
        row = conn.execute(text('SELECT state FROM jobs WHERE id=:id AND owner_id=:owner'),dict(id=jid,owner=owner)).first()
        if not row:
            raise JobError(404,'Job not found','job_not_found')
        if row[0] not in PENDING:
            raise JobError(409,'Job is already dispatched or terminal','state_conflict')
        conn.execute(text("UPDATE jobs SET state='cancelled',terminal_at=:terminal,cleanup_at=:cleanup,"
                          "metadata_expires_at=:metadata WHERE id=:id"),dict(id=jid,**terminal_values(now)))
    return {'id':jid,'status':'cancelled'}


def get_job(engine, owner, jid, now):
    with engine.connect() as conn:
        row = conn.execute(text('SELECT * FROM jobs WHERE id=:id AND owner_id=:owner'),dict(id=jid,owner=owner)).mappings().first()
        if not row:
            raise JobError(404,'Job not found','job_not_found')
        attempts = conn.execute(text('SELECT id,request_id,endpoint_id,started_at,ended_at,outcome,uncertain,usage '
                                     'FROM attempts WHERE job_id=:id ORDER BY started_at,id LIMIT 3'),dict(id=jid)).mappings().all()
        decision = conn.execute(text('SELECT data FROM decisions WHERE request_id=:id'),dict(id=row['last_decision_id'])).scalar()
    expired = bool(row['cleanup_at'] and datetime.fromisoformat(row['cleanup_at']) <= now)
    return dict(id=jid,status=row['state'],created_at=row['created_at'],deadline_at=row['deadline_at'],
                policy=json.loads(row['policy']) if row['policy'] else None,attempt_count=row['attempt_count'],
                result=None if expired else (json.loads(row['result']) if row['result'] else None),
                result_expired=expired or bool(row['content_deleted_at']),
                error=json.loads(row['error']) if row['error'] else None,
                decision=json.loads(decision) if decision else None,
                attempts=[dict(r)|{'usage':json.loads(r['usage']) if r['usage'] else None,
                                  'uncertain':bool(r['uncertain'])} for r in attempts],mode='demo')


def cleanup(engine, now):
    """Called by worker each cycle; no active payloads are removed."""
    with immediate(engine) as conn:
        conn.execute(text("UPDATE jobs SET payload=NULL,result=NULL,content_deleted_at=:at "
                          "WHERE cleanup_at<=:at AND content_deleted_at IS NULL "
                          "AND state IN ('succeeded','failed','expired','cancelled','unknown')"),dict(at=stamp(now)))
        mappings=list(conn.execute(text('SELECT id FROM jobs WHERE idempotency_expires_at<=:at'),dict(at=stamp(now))).scalars())
        for jid in mappings:
            conn.execute(text('UPDATE jobs SET idempotency_key=:key,body_digest=:empty,idempotency_expires_at=NULL WHERE id=:id'),
                         dict(id=jid,key='retired_'+uuid4().hex,empty=''))
        # Delete in FK order. Unknown is retained as metadata for seven days.
        params = dict(at=stamp(now))
        conn.execute(text('DELETE FROM emissions_estimates WHERE execution_id IN '
            '(SELECT id FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE metadata_expires_at<=:at))'),params)
        conn.execute(text('DELETE FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE metadata_expires_at<=:at)'),params)
        conn.execute(text('DELETE FROM decisions WHERE job_id IN (SELECT id FROM jobs WHERE metadata_expires_at<=:at)'),params)
        conn.execute(text('DELETE FROM jobs WHERE metadata_expires_at<=:at'),params)
        cutoff = stamp(now-timedelta(days=7))
        conn.execute(text('DELETE FROM emissions_estimates WHERE execution_id IN '
            '(SELECT id FROM attempts WHERE job_id IS NULL AND ended_at<:cutoff)'),dict(cutoff=cutoff))
        conn.execute(text('DELETE FROM attempts WHERE job_id IS NULL AND ended_at<:cutoff'),dict(cutoff=cutoff))
        conn.execute(text('DELETE FROM decisions WHERE job_id IS NULL AND created_at<:cutoff '
            'AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.request_id=decisions.request_id)'),dict(cutoff=cutoff))
        conn.execute(text("DELETE FROM grid_snapshots WHERE retrieved_at<:cutoff AND NOT EXISTS "
            "(SELECT 1 FROM decisions d,json_each(d.data,'$.snapshot_ids') s WHERE s.value=grid_snapshots.id)"),
            dict(cutoff=cutoff))
