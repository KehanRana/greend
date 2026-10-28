# Bounded inference trials

While GPU quota approval is pending, prepare a trial without launching services or sending
requests. The runner defaults to a **zero-network dry run**:

```sh
python scripts/run_trial.py
```

The default plan reserves three requests × 32 completion tokens = 96 requested completion
tokens. A fixed short synthetic prompt keeps content predictable. No credentials are needed
for planning, and `.env` is not automatically read.

## Execute against the local demo

Start the migrated demo gateway and mock using the README. Load your gateway credential
in the terminal, then explicitly execute:

```sh
python scripts/run_trial.py --execute > runtime/demo-trial.json
```

Execution first reads `/ready`, authenticated `/api/regions` and `/api/metrics`. Mode must match,
the model and fixed baseline must be eligible, and both the preferred endpoint and baseline
must have fresh signals. If these checks fail, no inference is submitted. A mode mismatch
cannot silently turn a demo trial into a live trial.

## Prepare a live trial

Do not activate live mode merely to run the planner. First complete the reviewed deployment
and isolated database procedure in [live integration](live-integration.md). After access,
locality/model reviews and a separate cost agreement, inspect the plan:

```sh
python scripts/run_trial.py --mode live --model shared-model \
  --requests 3 --max-tokens 32 --completion-token-budget 96
```

When the live gateway is ready and its credential is exported, adding `--execute` performs
the trial. Use `--base-url https://your-gateway.example.com` for an HTTPS gateway. Loopback
HTTP is accepted for local gateways; public HTTP, embedded credentials, URL paths, queries
and fragments are rejected. The runner talks only to the gateway, not directly to regional
providers or Electricity Maps. It disables redirects and environment proxy inheritance.

## Limits and evidence

- Maximum 20 sequential chat dispatches, at most 256 requested completion tokens each.
- The sum of requested completion caps must fit `--completion-token-budget`; this reserves
  capacity before dispatch and does not depend on reported usage to decide how many calls fit.
- This is a completion-token planning bound, **not** a dollar, input-token, total-token or
  provider-enforced spending limit. Upstream `max_tokens` enforcement is still required.
- In chat mode: no automatic retries, asynchronous jobs or fallback requests. Gateway interactive requests
  do not replay uncertain executions. The first HTTP error, interrupted request, invalid
  provenance, missing/inconsistent usage or reported completion-cap breach stops further calls.
- A timeout cannot retract compute already started upstream. Treat interrupted/5xx execution
  as uncertain. Do not rerun automatically to replace a missing response.

JSON reports contain preflight source timestamps/snapshot IDs, selected region, gateway and
provider completion IDs, HTTP status, latency and reported usage. They omit response content,
credentials and raw provider error bodies. Reports go to stdout; redirect into the Git-ignored
`runtime/` directory when persistence is needed (`mkdir -p runtime` first).
Exit code zero means dry-run success or all planned requests completed; other outcomes exit one.

Preflight signals are a moment-in-time check, not immutable dispatch-time emissions evidence.
The gateway records per-dispatch assumptions and estimates separately in `/api/activity`.
The runner does not calculate or claim measured avoided carbon; energy remains illustrative.
A live/provider failure report is useful evidence, not proof of model/locality equivalence.

Current integration tests execute this runner against the live gateway with mocked grid/vLLM
providers. No real inference trial was executed while GPU access remained unavailable.

## Bounded batch trials

The runner also supports sequential asynchronous jobs. Preview a batch plan with no network:

```sh
python scripts/run_trial.py --kind batch --trial-id pilot-001 \
  --allowed-region us-east-1 --allowed-region eu-central-1 \
  --max-carbon 150 --completion-token-budget 288
```

The default three jobs × 32 completion tokens × **three possible worker attempts** reserves
288 completion tokens. The worker and planner share the same attempt-limit constant.
`automatic_retries=false` describes the runner: the scheduler can still make its existing
bounded safe retries, and the reservation includes them. This is not a dollar/input-token cap.

Add `--execute` only with the intended gateway running and its credential exported. For a live
trial also set `--mode live --model YOUR_ALIAS` and use reviewed allowed regions. Batch preflight
requires matching mode and an eligible model/region; stale or above-threshold signals can
intentionally produce `waiting_for_carbon`. The threshold is enforced by the scheduler, using
its configured freshness policy. The runner does not relax that policy.

Jobs are submitted one at a time and polled to completion before submitting the next. Every
submission has an idempotency key derived from `--trial-id` and its index. There are no client
submission retries. The first failed/expired/cancelled/unknown job, polling error, mode mismatch
or exhausted polling allowance stops subsequent submissions. Each job uses `deadline_policy=expire`;
`--wait-seconds` (default 60, maximum 300) sets its waiting deadline and local polling horizon.
`--max-polls` (default 120) bounds total polls across the entire trial. Running execution can
finish after the waiting deadline or after local polling stops.

A timeout or polling stop does **not** cancel or retract an admitted job. Reports retain known
job IDs and idempotency keys. Inspect the existing job through its authenticated API before
starting another trial. Keep the same trial ID for uncertain submissions; do not replace them
with fresh keys. This runner does not resume a saved report. Re-running later rebuilds deadlines,
so an existing key may correctly return 409 due to its changed payload; that prevents duplicate
submission rather than resuming it. Idempotency expires under the normal gateway retention policy
(24 hours); keys are not permanent deduplication guarantees.

Batch reports include observed queue-state transitions, attempt identities/outcomes/usage,
selected endpoint, decision reason, snapshot IDs and immutable dispatch estimation assumptions,
plus provider completion ID for successful jobs. They omit prompts/results beyond the fixed
public plan prompt and omit raw error bodies/credentials. Energy assumptions remain illustrative.
Unlike chat preflight snapshots, the job decision captures dispatch-time comparison evidence.

Batch execution has been tested against the live gateway and worker using synthetic grid/vLLM
providers. No real batch inference was run while regional GPU access remained unavailable.
