# GreenRoute

Local, simulated inference gateway for the Micro-Grid Aware LLM Inference Scheduler project.

**Implemented:** SDK-compatible non-streaming text chat and persisted routing metadata (M1); timestamped grid fixtures, caching and carbon policies (M2); durable asynchronous jobs, owner-scoped polling/cancellation, independent worker, retries, leases, health and retention (M3).

**Planned:** emissions estimates/dashboard (M4), 500-job demonstration and performance validation (M5), conditional live-provider integration (M6). All regions point to one local mock. There is no real inference, energy measurement or verified carbon reduction.

## Quickstart

Run from the repository root with Python 3.12+. The lock is verified on Python 3.14.5/macOS arm64; other platforms require verification. Node is not required until dashboard implementation.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
cp .env.example .env
```

Choose a private gateway key in `.env`. Load configuration in **each terminal**:

```sh
set -a
source .env
set +a
export PYTHONPATH=backend
```

Apply migrations before starting or restarting services after an update:

```sh
python -m alembic upgrade head
```

Revision `002` adds grid mappings/quality; `003` adds job lifecycle and endpoint health metadata. Existing M1/M2 records are preserved. Downgrading deletes the corresponding schema/data; use it only on disposable databases.

Start the mock in terminal 1:

```sh
python -m uvicorn greenroute.mock:app --host 127.0.0.1 --port 8001
```

Start the gateway in terminal 2:

```sh
python -m uvicorn greenroute.app:create_app --factory --host 127.0.0.1 --port 8000
```

Start the worker in terminal 3:

```sh
python -m greenroute.worker
```

Run examples in terminal 4:

```sh
python examples/interactive.py
python examples/batch.py
python examples/batch.py --max-carbon 150
```

The default batch example chooses the cleanest eligible region immediately. The 150-threshold example may wait approximately three minutes for the Frankfurt fixture drop. It polls for up to 390 seconds; stopping the client does not cancel its durable job. Use `--base-url http://127.0.0.1:PORT` for a different gateway port.

API documentation: `http://127.0.0.1:8000/docs`. `/health` checks liveness; `/ready` checks schema/storage. Worker health and performance are not part of readiness yet.

## Synchronous contract

`POST /v1/chat/completions` requires `Authorization: Bearer <gateway-key>`. It accepts `model="demo-model"`, text `messages` with system/user/assistant roles, optional temperature (0–2), optional max_tokens (1–8192), and `stream=false`.

Unsupported fields/features return 422, malformed JSON 400, invalid credentials 401 and bodies larger than 1 MiB 413. Fixture output and usage are fixed: they do not measure the input or enforce generation limits.

Omitted `x-carbon-priority` or `high` routes immediately by configured latency rank. Deferred/threshold headers return 422 directing callers to `/api/jobs`. Unknown models have no eligible endpoint and return 503. No grid fetch occurs during interactive request handling.

The gateway supplies a distinct upstream credential and only allowlisted metadata. Successful responses carry `x-greenroute-request-id`, `x-greenroute-region` and `x-greenroute-mode`. Upstream errors are sanitized (429/502/504); interrupted or invalid responses can be recorded as unknown. Synchronous calls are not automatically retried.

## Durable job contract

`POST /api/jobs` requires a client bearer key and an `Idempotency-Key` of 1–128 letters, digits or `._:-`. It returns 202 only after commit, with `id`, `status` and `status_url`.

```json
{
  "request": {
    "model": "demo-model",
    "messages": [{"role": "user", "content": "Summarize this document."}]
  },
  "policy": {
    "allowed_regions": ["us-east-1", "eu-central-1", "ap-southeast-2"],
    "max_carbon_gco2e_kwh": 150,
    "deadline_at": "REPLACE_WITH_A_FUTURE_UTC_TIMESTAMP",
    "deadline_policy": "expire"
  }
}
```

The deadline must be future and within 24 hours on **new admission**. Parsing stored policies is separate from this check, allowing recovery after a deadline. Repeating the same normalized body and owner/key within 24 hours returns the original identity/state, even after its deadline. Different content returns 409. After key expiry it can create new work; clients must not rely on deduplication beyond 24 hours.

| Endpoint | Behavior |
|---|---|
| `GET /api/jobs/{id}` | Owner-only state, policy, latest transition/dispatch decision, up to three attempts, result and sanitized error |
| `POST /api/jobs/{id}/cancel` | Atomically cancel queued, waiting or retry-pending work; 409 after dispatch or terminal state |
| `GET /api/jobs?limit=20&offset=0` | Owner-only metadata list; no payloads or results; maximum limit 100 |
| `GET /api/activity?limit=20&offset=0` | Shared authenticated routing/attempt metadata, without prompts or completion content |
| `GET /api/regions` | Shared authenticated registry, health, grid provenance and freshness |

A missing job and another owner's job both return 404. Capacity exhaustion returns 429 with retry guidance; storage failure returns 503, so clients can retry with the same key/body. The default pending cap is 1,000 across owners; running jobs are separately limited.

Jobs move through queued/waiting/running/retry-pending to succeeded, failed, expired, cancelled or unknown. Cancellation does not promise to stop already dispatched upstream work. A deadline bounds waiting, not completion of an already dispatched call.

## Worker, retries and recovery

Run one worker process for the MVP. It shares the gateway's database and reads grid snapshots written by the gateway. It reevaluates waiting work every ten seconds and wakes sooner on task completion to fill free slots.

Claims, capacity checks and cancellation use short `BEGIN IMMEDIATE` transactions. No provider call runs inside a database write transaction. At most ten jobs are running across claims; interactive calls have a separate per-gateway cap of ten. Total inference concurrency can therefore reach twenty with default settings.

A claim has a random owner token and a renewable lease. The default 120-second lease must exceed the 60-second total inference timeout. A completion requires current, unexpired ownership and writes the attempt/result/state atomically. Pending jobs survive API/worker restarts. Expired running leases become **unknown**, with no automatic replay. Stale owners cannot commit late results. Abandoned synchronous attempts are also conservatively marked unknown after timeout plus lease duration.

Only classified pre-connection failures and explicit 429 rejections may retry. Backoff starts at two seconds, honors numeric/HTTP-date Retry-After guidance, allows at most three total attempts and never schedules a retry at or beyond the deadline. Ambiguous read/write failures, 5xx responses and malformed successful responses become unknown. Configuration/auth/model errors fail and disable the endpoint. Graceful worker shutdown drains current calls; a hard crash leaves leases for recovery.

Three consecutive endpoint connection/5xx failures open its circuit. The worker probes after 30 seconds; a successful health probe restores eligibility. Configuration errors need correction and explicit operator reenablement. A probe returning 401/403/404 also requires operator correction.

## Credentials, operations and retention

The primary gateway key maps to owner `local`. Optional `GREENROUTE_CLIENT_KEYS` is a JSON mapping of stable client names to distinct bearer keys; `.env.example` shows shell quoting. Job output and cancellation are owner-scoped. Regional/activity metadata remains shared among authenticated clients; this is a local prototype rather than a production tenant-isolation system.

An optional **distinct** `GREENROUTE_OPERATOR_KEY` enables `POST /api/operator/endpoints/{id}/enable` after correcting endpoint configuration. A client key cannot perform this action. Without an operator key the mutation is unavailable. Live mode and live providers remain unsupported; scenario-mutation controls are deferred to the full demo milestone.

The SQLite file is owner-only. Queued content is stored locally for execution; never commit `.env` or runtime databases. Logs and metadata endpoints omit prompts, completion bodies and raw provider errors. Only an authorized job-result read returns completion content.

During worker cycles, terminal payloads/results are logically deleted after 24 hours and content-free job/attempt metadata after seven days. Result reads hide expired output even before cleanup runs. Idempotency keys/digests expire after 24 hours from submission. Active job content is retained; grid snapshots needed by retained decision evidence remain available. Logical deletion is not secure disk erasure; storage encryption is a deployment responsibility. Keep the worker running for physical cleanup.

## Regional signals and offline policy demonstration

The gateway polls fixtures at startup and every 60 seconds. Signals older than 300 seconds are stale; exactly 300 seconds still qualifies. Invalid, missing, negative, non-finite or future observations cannot qualify. Retrieval time never rejuvenates an old observation. Failed refreshes keep cached data and back off to at most 15 minutes, with a five-second fetch timeout.

```sh
curl -H "Authorization: Bearer $GREENROUTE_GATEWAY_KEY" http://127.0.0.1:8000/api/regions
python examples/carbon_policy.py
```

The fixture starts at Virginia 400, Frankfurt 300 and Sydney 500 gCO₂e/kWh. Frankfurt drops to 80 after 180 seconds, visible on the next poll. Each gateway restart begins a new simulated timeline. `demo:` zone IDs are not verified Electricity Maps mappings.

The offline script advances its own clock instantly: **wait → select Frankfurt → expire** at independent evaluation times 0, 180 and 200 seconds. It does not submit jobs or invoke inference.

Flexible routing enforces allowed regions, matching model/version, health and simulated locality before optimizing fresh intensity. Ties use latency rank then endpoint ID. `expire` blocks dispatch at the deadline. `execute_best_available` selects the best fresh eligible region above threshold if needed, or latency rank when no fresh signal exists. Exceptions are explicit; no eligible region means wait before deadline and expire at it.

Unchanged waiting decisions are coalesced; the job detail shows evidence of the last changed waiting decision. Every actual dispatch records a new decision with exact snapshot references.

## Configuration defaults

| Variable | Default |
|---|---|
| `GREENROUTE_GRID_REFRESH_SECONDS` | 60 |
| `GREENROUTE_GRID_MAX_AGE_SECONDS` | 300 |
| `GREENROUTE_GRID_TIMEOUT_SECONDS` | 5 |
| `GREENROUTE_PENDING_JOB_LIMIT` | 1000 |
| `GREENROUTE_WORKER_CONCURRENCY` | 10 |
| `GREENROUTE_INTERACTIVE_CONCURRENCY` | 10 |
| `GREENROUTE_WORKER_POLL_SECONDS` | 10 |
| `GREENROUTE_RETRY_BASE_SECONDS` | 2 |
| `GREENROUTE_LEASE_SECONDS` | 120 |

## Verification

```sh
python -m pytest -q
```

Tests use isolated temporary databases, deterministic clocks and real local process checks. See [M1 verification](docs/m1-verification.md), [M2 verification](docs/m2-verification.md) and [M3 verification](docs/m3-verification.md) for evidence and boundaries. Load/latency targets and real grid/provider locality remain unverified.
