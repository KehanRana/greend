# Isolated live runtime (M6 / B-15)

Status: gateway, worker, adapters and mode isolation are implemented and tested with synthetic
HTTP providers. Real-provider feasibility is **pending**: no provider access, regional deployments,
approved zone mappings or credentials were supplied. No real grid or inference requests have
been made during this implementation. Live energy estimates remain illustrative.

## Review the deployment manifest

Copy `examples/live-endpoints.example.json` to a local manifest. Replace every example host,
region/zone placeholder, model revision and locality evidence reference. Use the same public
model alias and immutable revision across deployments; served model names may differ.
Manifest order specifies interactive latency preference; it is administrator configured,
not automatically measured. Select an explicit fixed baseline in the manifest and settings.

A reviewer must establish regional processing locality and model/template equivalence before
setting both `locality_verified` and `model_equivalence_verified` to `true` for every deployment.
These default to false. They record administrator attestations, not automated verification.
A model-discovery probe cannot prove locality or equivalence. Example placeholders cannot
be activated. No AWS-to-grid-zone mappings are inferred.

```sh
python scripts/check_live_config.py examples/live-endpoints.example.json
```

This checker always makes zero network requests. It checks structure and credential presence,
reports review booleans and never emits credentials, URLs or provider responses. A structurally
valid example is not live-ready. Do not put keys in JSON or URLs; manifests contain only
credential environment variable names. Keep manifests private if evidence references are sensitive.

## Initialize a separate database

Only perform these steps after access, regional reviews, API entitlement/rate limits and an
inference budget are established. Commands below are the activation procedure, not a report
that activation has occurred. Use a separate terminal/environment from the demo processes.
The application does not automatically load `.env` files.

```sh
export PYTHONPATH=backend
export GREENROUTE_MODE=live
export GREENROUTE_DATABASE=runtime/live.db
export GREENROUTE_LIVE_MANIFEST=/absolute/path/to/reviewed-endpoints.json
export GREENROUTE_BASELINE_ENDPOINT=region-a
# Set GREENROUTE_GATEWAY_KEY, optional distinct GREENROUTE_OPERATOR_KEY,
# GREENROUTE_ELECTRICITYMAPS_API_KEY and each manifest-declared vLLM key securely.
alembic upgrade head
python -m greenroute.runtime
uvicorn greenroute.app:create_app --factory --host 127.0.0.1 --port 8000
# In another terminal with the same live configuration:
python -m greenroute.worker
```

Migration 005 adds a database runtime identity, defaulting existing databases to demo.
Initialization makes no network requests and replaces seeded demo endpoints only when the
migrated database has no jobs, decisions, attempts, snapshots or estimates. It refuses to
repurpose a database with execution evidence. Reinitialization with the same manifest is
idempotent; a changed manifest requires a new live database. The default demo database path
is disallowed for live settings. Never run live migration commands against the demo database.

Startup binds a reviewed manifest digest to the database. Gateway and worker reject mismatched
modes, manifest digests or registry metadata. Each running process retains its approved manifest
in memory: changing the file does not redirect active traffic. Restarting with the changed file
fails the digest check. Credential values are resolved from the environment, not persisted.
Health/circuit fields remain mutable; operator enable cannot bypass registry approval.

Starting the live gateway refreshes Electricity Maps immediately and periodically. Submitted
chat requests and eligible jobs call the configured vLLM deployments and may incur cost.
Initialization and the offline checker do not perform discovery or billed inference.

## Runtime behavior

Interactive chat routes to the first healthy, approved equivalent model by configured preference.
Flexible jobs preserve region restrictions, freshness, carbon thresholds, deadlines, conservative
retry rules and lease recovery. Batch jobs wait when signals are stale/unavailable unless their
explicit deadline policy permits best-available execution. Latency-critical chat can proceed
without a fresh carbon signal, with unavailable carbon comparisons explicitly excluded.

The live gateway uses authenticated Electricity Maps v4 `/carbon-intensity/latest` with explicit
zones, lifecycle factors, flow-traced consumption accounting and IP lookup disabled. It preserves
`datetime` as the observation time; `updatedAt` is not substituted. Mismatched zones/accounting,
invalid values and future/naive/stale timestamps reject the whole fetched batch. Provider errors
retain the timestamped cache with bounded polling backoff. There is no demo fallback.
Freshness defaults to 300 seconds: align this with purchased granularity and availability before
a trial. Hourly data may legitimately fail that limit. Poll timeout bounds the entire batch.

Regional vLLM calls map the public alias to a served model name, disable redirects and use only
the regional credential plus gateway request identity. Nonstreaming text-chat validation and
usage/error contracts are shared with the demo adapter. 401/403/404 disable a destination pending
operator intervention; 429 follows bounded retry guidance for jobs; uncertain 5xx/transport
outcomes are not automatically replayed. Circuit recovery checks authenticated `/v1/models`
for the exact registered model; it does not generate a completion.

## Data and UI isolation

A database belongs to one mode. Grid cache reads filter by mode, routing requires matching
snapshot provenance, and decisions, response headers, jobs, activity and estimates report that
mode. Metrics default to the configured mode and exclude the other mode, including queue counts.
Live usage is labeled `provider_reported_usage`; demo usage remains `simulated_usage`.
Energy coefficients remain illustrative in both modes, and estimates are not measured avoided
emissions. Failed/unknown executions do not contribute successful carbon comparisons.

The existing dashboard and scenario controls are demo-specific. `/dashboard/` and demo controls
are unavailable in live mode; use authenticated JSON APIs (`/api/regions`, `/api/jobs`,
`/api/activity`, `/api/metrics`) for live inspection. A live dashboard is future work.

## Validation and remaining trial gates

```sh
python -m pytest tests/test_live.py tests/test_live_runtime.py -q
python -m pytest -q
```

Tests use `httpx.MockTransport`, synthetic responses, isolated databases and deterministic clocks.
They cover live chat/worker dispatch, dirty-grid waiting/release, auth/rate-limit/outage paths,
stale-cache retention, model mapping, mode/manifest/registry rejection, disabled demo controls,
cache/metrics provenance isolation and read-only model recovery. They are not recorded provider
responses, real locality evidence or real feasibility results.

Before a real trial: confirm entitlement/rate limits and zone mapping, review locality and model
revision/template equivalence, agree a bounded token/request budget, probe discovery/grid access,
then submit an authorized bounded completion. Record actual provider IDs, usage and timestamps
and compare against the fixed baseline. Calibrate energy separately before claiming measured
emissions. Do not present synthetic validation as an actual-provider milestone completion.

API contracts checked 2026-10-28 against primary documentation:
- [Electricity Maps v4 latest carbon intensity](https://app.electricitymaps.com/docs/reference/carbon-intensity/latest)
- [Electricity Maps authorization/reference](https://app.electricitymaps.com/docs)
- [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)
