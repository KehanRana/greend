# GreenRoute

Local, simulated inference gateway for the Micro-Grid Aware LLM Inference Scheduler project.

**Implemented:** SDK-compatible non-streaming text chat and persisted routing metadata (M1); timestamped grid fixtures, caching and carbon policies (M2); durable asynchronous jobs, owner-scoped polling/cancellation, independent worker, retries, leases, health and retention (M3).

**Implemented M4:** versioned per-execution estimates, aggregate APIs, React dashboard, persistent grid scenarios and resumable synthetic batch replay.

**Implemented M5:** isolated performance/failure verification, fresh lockfile-install rehearsal, requirements traceability and demo/recovery guides.

**Remaining:** conditional live-provider integration (M6), subject to endpoint/grid access and locality/energy evidence. All regions point to one local mock. There is no real inference, energy measurement or verified carbon reduction.

## Quickstart

Run from the repository root with Python 3.12+. The lock is verified on Python 3.14.5/macOS arm64; other platforms require verification. The dashboard also requires Node/npm (verified with Node 22.14.0 and npm 10.9.2).

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
npm ci --prefix frontend
npm run build --prefix frontend
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

Revision `002` adds grid mappings/quality; `003` adds job lifecycle/health; `004` adds estimate metadata and persistent scenarios. Existing M1/M2 records are preserved. Downgrading deletes the corresponding schema/data; use it only on disposable databases.

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

Dashboard: `http://127.0.0.1:8000/dashboard/`. Build before starting the gateway; the built dashboard shares its origin. Existing installations should preserve their `.env`, migrate and restart services after updating.

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

Three consecutive endpoint connection/5xx failures open its circuit. The worker probes after 30 seconds; a successful health probe restores eligibility. Configuration errors need correction/restart and explicit operator reenablement. A probe returning 401/403/404 also requires operator correction.

## Credentials, operations and retention

The primary gateway key maps to owner `local`. Optional `GREENROUTE_CLIENT_KEYS` is a JSON mapping of stable client names to distinct bearer keys; `.env.example` shows shell quoting. Job output and cancellation are owner-scoped. Regional/activity metadata remains shared among authenticated clients; this is a local prototype rather than a production tenant-isolation system.

An optional **distinct** `GREENROUTE_OPERATOR_KEY` enables `POST /api/operator/endpoints/{id}/enable` after correcting endpoint configuration. A client key cannot perform this action. Without an operator key the mutation is unavailable. Live mode and live providers remain unsupported. Scenario changes additionally require operator authorization and demo mode.

The SQLite file is owner-only. Queued content is stored locally for execution; never commit `.env` or runtime databases. Logs and metadata endpoints omit prompts, completion bodies and raw provider errors. Only an authorized job-result read returns completion content.

During worker cycles, terminal payloads/results are logically deleted after 24 hours and content-free job/attempt metadata after seven days. Result reads hide expired output even before cleanup runs. Idempotency keys/digests expire after 24 hours from submission. Active job content is retained; grid snapshots needed by retained decision evidence remain available. Logical deletion is not secure disk erasure; storage encryption is a deployment responsibility. Keep the worker running for physical cleanup.

## Regional signals and offline policy demonstration

The gateway polls fixtures at startup and every 60 seconds. Signals older than 300 seconds are stale; exactly 300 seconds still qualifies. Invalid, missing, negative, non-finite or future observations cannot qualify. Retrieval time never rejuvenates an old observation. Failed refreshes keep cached data and back off to at most 15 minutes, with a five-second fetch timeout.

```sh
curl -H "Authorization: Bearer $GREENROUTE_GATEWAY_KEY" http://127.0.0.1:8000/api/regions
python examples/carbon_policy.py
```

The fixture starts at Virginia 400, Frankfurt 300 and Sydney 500 gCO₂e/kWh. Frankfurt drops to 80 after 180 seconds, visible on the next poll. The default gateway persists its scenario and start time across restarts. The standalone offline fixture example still uses a fresh private clock. `demo:` zone IDs are not verified Electricity Maps mappings.

The offline script advances its own clock instantly: **wait → select Frankfurt → expire** at independent evaluation times 0, 180 and 200 seconds. It does not submit jobs or invoke inference.

Flexible routing enforces allowed regions, matching model/version, health and simulated locality before optimizing fresh intensity. Ties use latency rank then endpoint ID. `expire` blocks dispatch at the deadline. `execute_best_available` selects the best fresh eligible region above threshold if needed, or latency rank when no fresh signal exists. Exceptions are explicit; no eligible region means wait before deadline and expire at it.

Unchanged waiting decisions are coalesced; the job detail shows evidence of the last changed waiting decision. Every actual dispatch records a new decision with exact snapshot references.

## Dashboard and estimates

Open `/dashboard/`, enter your client gateway key and connect. Credentials remain in browser memory only, with no local/session storage. The overview has a regional map, comparison totals, recent decisions and your pending queue. Separate queue/evidence views keep the main page short. Click a region or request to inspect its stored evidence. Pending jobs can be cancelled from the queue.

The frontend polls metadata every three seconds, retains last-good values on refresh failure and marks them as cached. It uses locally bundled IBM Plex Sans and Natural Earth geographic data; licenses are included in the dashboard assets. The map shows endpoint locations, not verified electricity-zone boundaries. The comparison curve shows the most recent completed default-to-selected comparison available in the recent activity slice, not a measured network path.

`GET /api/metrics?mode=demo` requires a client key and reports retained workspace execution counts, comparable/excluded estimates, signed grams, exclusion reasons, assumption groups and owner-specific job states. `mode=live` is segregated; no live data is generated. Activity rows include the stored estimate and immutable dispatch context. Metadata is shared, while job results remain owner-scoped.

For each successful execution:

```
estimated kWh = (prompt tokens + completion tokens) / 1,000 × configured coefficient
estimated gCO₂e = estimated kWh × dispatch-time grid intensity
difference = fixed-baseline estimate − selected-route estimate
```

The default baseline is `us-east-1`; the illustrative coefficient is 0.001 kWh per 1,000 total tokens. The selected/default grid snapshots, their validity, model/version, coefficient/version and baseline identity are bound before dispatch. Later settings or grid changes do not rewrite those assumptions. Successful completion persists the estimate in the same transaction; a unique execution identity prevents double counting.

Missing usage, stale/missing signals and an ineligible default region make the comparison unavailable. These executions appear in excluded counts rather than as measured zeros. Primary totals cover comparable successful executions only; negative differences remain negative. Historical pre-M4 executions without dispatch evidence are excluded, not backfilled from current data. A secondary worst-region comparison is stored in each available estimate's assumptions and is never substituted for the primary baseline.

All current usage and grid data is simulated. The energy model is illustrative; the estimate excludes failed attempts, networking, cooling, embodied energy and temporal-shifting benefits. It does not establish real avoided emissions, offsets or reporting compliance.

For frontend development, keep the Python gateway running and use:

```sh
npm run dev --prefix frontend
```

Open the printed `/dashboard/` URL. Vite proxies `/api` to port 8000; set `GREENROUTE_API_ORIGIN` for another local gateway. Production uses `npm run build --prefix frontend` and the gateway's static mount. No third-party font or map requests are needed.

## Demo scenarios and 500-job replay

In the dashboard, **Grid scenario** accepts the separate operator key:

- `high`: all three regions remain above the 150 threshold.
- `clean`: Frankfurt is 80 gCO₂e/kWh.
- `scheduled`: Frankfurt drops after 180 seconds from this scenario's persisted start time.

`POST /api/demo/scenario` takes `{ "scenario": "high" | "clean" | "scheduled" }`; `GET /api/demo/scenario` is client-readable metadata. Applying a scenario changes future grid observations across the workspace and refreshes the cache; it never deletes jobs, results or totals. If refresh fails, the response reports it. Live-mode mutation is unavailable.

The dashboard submits up to 100 synthetic jobs at a time. An unconfirmed submission can be retried with the same saved key/body while its dialog remains open. For a resumable 500-job demonstration, set the operator key in the environment and run:

```sh
python examples/demo_batch.py --count 500 --release-clean --run-file runtime/demo-run.json
```

This applies high signals, submits jobs, waits until they are waiting, applies the clean scenario and polls until terminal. It changes the shared demo grid and can release other waiting jobs, so use a dedicated demo workspace for isolated run totals. The JSON output labels **this-run state counts** separately from **workspace estimate totals**.

The run file contains synthetic bodies, deadlines and stable idempotency keys, with owner-only file permissions and no credentials. Repeating the same command resumes those stored bodies instead of generating new jobs within the 24-hour key window. Existing files determine the batch size; choose a new filename for a new run. Polling is bounded, and accepted work remains durable if the client stops.

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
| `GREENROUTE_MOCK_DELAY_SECONDS` | 0 (verification uses 0.1) |
| `GREENROUTE_BASELINE_ENDPOINT` | us-east-1 |
| `GREENROUTE_ENERGY_KWH_PER_1000_TOKENS` | 0.001 |
| `GREENROUTE_ENERGY_MODEL_VERSION` | illustrative-v1 |

## Verification

```sh
python -m pytest -q
npm run test --prefix frontend
npm run build --prefix frontend
```

Tests use isolated temporary databases, deterministic clocks and real local process checks. See [M1 verification](docs/m1-verification.md), [M2 verification](docs/m2-verification.md) and [M3 verification](docs/m3-verification.md) and [M4 verification](docs/m4-verification.md) for evidence and boundaries. Load/latency targets and real grid/provider locality remain unverified.


## Verified local targets and handoff

On the recorded Apple M4 run, 1,000 matched direct/gateway pairs at concurrency ten gave **28.784 ms p95 added RTT** against the 100 ms target. A 500-job batch with a nominal 100 ms mock and ten worker slots drained in **16.172 seconds** against the 300-second target. The measurement subtracts each pair's RTTs; it is not a production latency guarantee.

A fresh source-copy/venv/npm rehearsal passed **106 backend tests, 7 UI tests**, build and real-process SDK/job/restart/dashboard smoke. Process-fault checks cover worker SIGKILL after provider receipt, provider outage/recovery, 429 timing, admission write failure and stale-grid behavior. No optimization or SLO claim for a real regional provider follows from these results.

```sh
python scripts/run_verification.py
python scripts/rehearse.py
```

Both commands use disposable databases and their own loopback processes/credentials. They do not use your live `.env` or existing services. The full runner takes about a minute on the recorded machine. Rehearsal installs pinned dependencies into a new temporary source tree. Use `--output runtime/my-report.json` to preserve the checked-in reports.

- [Verification results and repeatable methodology](verification/README.md)
- [Requirements-to-evidence matrix](verification/requirements-matrix.md)
- [Architecture and transaction boundaries](guides/architecture.md)
- [Operations and safe recovery](guides/operations.md)
- [Demo walkthrough and handoff](guides/demo.md)

The test-only `verification.fault_*` factories are not normal demo entry points. Live integration remains gated; no real grid/provider credentials, energy calibration or production deployment are included.
