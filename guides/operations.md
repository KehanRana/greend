# Local operations and recovery

Use the root README for initial install/build commands. Keep your environment loaded in each service terminal and run from the repository root. Normal entry points are `greenroute.app:create_app`, `greenroute.mock:app` and `greenroute.worker`.

`verification.fault_gateway` and `verification.fault_upstream` are test-only service factories used by the isolated verification runner. They are not normal demo entry points.

## Start and check

1. Activate the Python environment, load `.env`, and export `PYTHONPATH=backend`.
2. Apply `python -m alembic upgrade head`; current schema is `006`.
3. Build with `npm run build --prefix frontend` before starting the gateway.
4. Start mock, gateway and worker using the README commands, each on the same configured database/keys.
5. Check `/health` and `/ready`; open `/dashboard/` and enter the client key.

Liveness means the API process is serving. Readiness checks schema/storage, not provider reachability, worker liveness or carbon freshness. Inspect `/api/regions`, `/api/metrics` and the owner queue for those conditions. A gateway can be ready while its grid source is degraded.

Use `GREENROUTE_MOCK_DELAY_SECONDS=0.1` to introduce the benchmark's nominal 100 ms synthetic response delay; default normal-demo delay is zero. This setting changes no real model computation.

## Diagnose a waiting job

Read `GET /api/jobs/{id}` with its owner's key. The response includes policy, state, attempts and the latest changed waiting/dispatch decision. Check allowed regions, model/version, endpoint health, signal freshness, threshold and deadline in that order.

A healthy region with a fresh signal can still exceed the threshold. Do not clear the queue to fix this. In demo mode, an operator can apply the clean scenario; for other workloads, wait or use the explicitly selected deadline policy. Missing/stale observations cannot qualify before deadline.

Unknown/ineligible models and unavailable regions can wait until deadline. `expire` prevents dispatch at the deadline; `execute_best_available` may explicitly override threshold/freshness but cannot override model/geographic eligibility. A dispatched call may finish after the waiting deadline.

## Restart and uncertainty

Graceful worker shutdown drains current bounded calls; default call timeout is 60 seconds. A hard crash leaves running leases. Start a worker with the same database and let expired leases become unknown. Default lease is 120 seconds and renews during active calls, so recovery depends on the last renewal time.

An unknown job may already have reached the provider. Inspect its attempt/request IDs and any upstream receipt before deciding on a new execution. Do not change unknown to queued in SQL. A replacement is an explicit new job and may duplicate provider work; no automatic reconciliation or replay is implemented.

Pending jobs survive API and worker restarts. Acknowledgment is only sent after commit. If a submission times out or receives storage 503, retry the same owner, idempotency key and normalized body. Changed content under that key returns 409. Key mappings expire 24 hours after submission; deduplication is not promised afterward.

## Provider failures

Pre-connection failures and explicit 429 rejections may retry at most three total attempts, respecting backoff, Retry-After and deadline. Read/write interruption, malformed success and 5xx responses are treated conservatively as uncertain. Request rejection/configuration errors fail without speculative replay.

Three endpoint connection/5xx failures open a circuit. The worker probes after 30 seconds. Configuration/auth/model failures require correction and operator reenablement; correct environment configuration and restart affected services first.

```sh
curl -X POST -H "Authorization: Bearer $GREENROUTE_OPERATOR_KEY" \
  http://127.0.0.1:8000/api/operator/endpoints/eu-central-1/enable
```

This is operator-only. A client key is deliberately insufficient. The endpoint must be corrected before reenablement or the next request can disable it again.

## Grid outages

The gateway retains cached observations with their original source times and backs off polling. Last-good data is not silently labeled live. Flexible work waits when no eligible fresh signal exists; interactive work still uses endpoint health/rank immediately.

A scenario change reports whether cache refresh succeeded. If it did not, its new persistent state is stored but old cache data remains until a successful refresh. The dashboard retains failed-refresh values with a warning. Restarting the gateway preserves the scenario timeline rather than resetting it.

## Capacity and storage

The default pending cap is 1,000 and worker running cap is ten. Queue or interactive saturation returns 429 with retry guidance. Increasing caps is an operational change, not a measured scaling guarantee. A rejected database write returns 503 and must not be treated as accepted.

Preserve the database for recovery. Do not delete or downgrade a working database to resolve an error. For an independent demo, choose a new `GREENROUTE_DATABASE` path and migrate it; retain the old file separately. There is no built-in backup/restore or HA service.

The database is owner-readable/writable only. Keep runtime files and `.env` out of Git. Remote exposure requires a separate deployment/security design; current evidence is for loopback use.

## Retention and estimates

A running worker deletes terminal payloads/results after 24 hours and metadata after seven days. Result reads hide expired output even before cleanup. Idempotency mappings expire after 24 hours from submission. Active payloads and grid snapshots needed by retained evidence are preserved. Deletion is logical SQLite deletion, not secure disk erasure; disk encryption remains a deployment responsibility.

Excluded estimates are visible through reason counts. Missing usage or stale/ineligible baseline evidence is not a measured zero. Old records without dispatch evidence stay excluded. Changing the coefficient/baseline affects future dispatches, not prior estimates. The default demo uses simulated grid, usage and inference data. The isolated live runtime labels provider usage separately; its energy coefficient remains illustrative. See [live setup](live-integration.md).

Live operation requires its own initialized database and reviewed manifest. Follow [the live activation procedure](live-integration.md); do not change a running demo process to live mode or repurpose a database containing demo records.

Grid granularity and freshness are separate live settings. `/api/regions` and metrics
configuration expose the requested granularity, nominal interval, source-time maximum age and
policy warnings. Missing or mismatched source granularity cannot qualify. Historical snapshots
are not backfilled with guessed granularity; the live poller must obtain a matching observation.
See [freshness setup](grid-access.md#granularity-and-scheduling-freshness).
