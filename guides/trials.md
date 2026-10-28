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
- No automatic retries, asynchronous jobs or fallback requests. Gateway interactive requests
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
