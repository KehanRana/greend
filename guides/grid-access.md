# Check Electricity Maps access without GPU infrastructure

The checker performs only authenticated GET requests to the official Electricity Maps v4 API.
It does not start GreenRoute, call inference endpoints, change runtime mode, or write to a database.
Keep `GREENROUTE_MODE=demo` while AWS GPU quota approval is pending.

From the repository root, use the existing Python environment:

```sh
source .venv/bin/activate
python scripts/check_grid_access.py --env-file .env
```

The command reads `GREENROUTE_ELECTRICITYMAPS_API_KEY` from the environment, or optionally the
named assignment in `.env` if it is absent from the environment. It never executes the file,
loads other settings, expands shell substitutions, or prints the key. `.env` is Git-ignored.
You can omit `--env-file` when the key is already exported.

With no zones, it makes one `/v4/zones` request and lists zones declaring carbon-intensity
access. Discovery success alone is not proof that a particular signal is available: `/zones`
is a discovery endpoint that can also be queried without authentication. Test a signal next.

```sh
python scripts/check_grid_access.py --env-file .env \
  --zone DE --zone AU-VIC --zone US-MIDA-PJM
```

These are sample signal zones, not approved AWS region mappings. Determine the correct grid
mapping from verified deployment locality before using any zone for scheduling.

The checker sends one discovery request plus one latest-carbon-intensity request per known
requested zone (up to ten unique zones). It explicitly disables IP-based lookup, requests
lifecycle, flow-traced consumption intensity and refuses redirects. There are no retries;
authentication or rate-limit rejection stops further signal requests. A timeout bounds each
request, defaulting to ten seconds. Raw provider error bodies and HTTP exception details are
never printed. Returned JSON includes only interpreted access and signal fields.

Results distinguish `unknown_zone`, `authentication_failed`, `access_denied`, `rate_limited`,
`not_found`, `provider_error`, `timeout`, `connection_failed`, `invalid_response`, `fresh`,
`stale` and `future`. Zone discovery permissions are declarations; a successful latest request
provides the direct access test. Failed checks exit with code 1; successful discovery or all-fresh
requested signals exit with code 0. Missing credentials cause zero requests.

Freshness uses the source `datetime`, not retrieval time or `updatedAt`. The default maximum
age is 300 seconds, matching the scheduler default. Hourly signals may be accessible but stale
under that policy. Use `--max-age-seconds 7200` to inspect a different explicit diagnostic limit;
this does **not** change the gateway's configured freshness policy. Set a scheduling limit only
after reviewing the signal's granularity and intended behavior.

To retain a sanitized local report, redirect stdout into the ignored runtime directory:

```sh
mkdir -p runtime
python scripts/check_grid_access.py --env-file .env --zone DE > runtime/grid-access-check.json
```

No live database initialization, locality approval or calibrated energy claim follows from
this check. See [live integration](live-integration.md) for the remaining deployment gates.

Contracts: [zone discovery](https://app.electricitymaps.com/docs/reference/helpers),
[authorization](https://app.electricitymaps.com/docs/quickstart/authorization),
[latest intensity](https://app.electricitymaps.com/docs/reference/carbon-intensity/latest).

## Granularity and scheduling freshness

Electricity Maps v4 supports `hourly`, `15_minutes` and `5_minutes`. Select the exact source
interval when checking access:

```sh
python scripts/check_grid_access.py --env-file .env --zone DE \
  --granularity hourly --max-age-seconds 300
```

The checker sends `temporalGranularity` explicitly and compares the returned value. A missing,
unknown or different value reports `granularity_mismatch`, never `fresh`. Successful discovery
does not guarantee that your subscription/zone supports each granularity; unsupported requests
remain access/provider errors and do not fall back to hourly data.

Live scheduling uses two independent environment settings:

```sh
GREENROUTE_GRID_TEMPORAL_GRANULARITY=hourly
GREENROUTE_GRID_MAX_AGE_SECONDS=300
```

Defaults deliberately remain hourly / 300 seconds. If the maximum age is shorter than the
nominal source interval, metadata warns `freshness_window_shorter_than_source_interval`.
This is allowed as a strict policy; it can make data stale through most of each interval.
Granularity describes the observation buckets, not a guarantee of when new data is published.
Polling more often does not make an older source timestamp fresh.

To evaluate hourly data with an explicit two-hour diagnostic allowance, use
`--granularity hourly --max-age-seconds 7200`. This does not modify `.env` or scheduling policy.
A live policy can separately be configured up to 7200 seconds after reviewing the desired
staleness tolerance, publication lag and API entitlement. No default is widened automatically.
Alternatively, choose finer-grained data if your API access supports it. Do not shorten a
nominal interval in configuration unless the provider actually returns that granularity.

Migration 006 stores returned granularity in each snapshot. Live cache selection filters by
mode and requested granularity before choosing the latest observation, so a newer observation
from another interval cannot replace the intended signal. Historical snapshots retain unknown
granularity and are ineligible for live selection until a matching refresh succeeds. There is
no need to reset the database. Apply `python -m alembic upgrade head` before restarting.

`/api/regions` and `/api/metrics` expose the active policy. Dispatch estimation assumptions
capture both the source granularity and configured maximum age, preserving the policy used for
that execution even if settings change later. Demo fixture timing and routing remain unchanged.
