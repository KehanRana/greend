# Live integration preparation (M6 / B-15)

Status: adapters and offline contracts implemented; live feasibility is **pending**.
No provider access, regional deployments, approved zone mappings or credentials were supplied.
The gateway and worker still accept demo mode only. These adapters are library boundaries,
not a switch that enables live routing. This prevents real usage or signals entering the
simulated dashboard, cache and emissions ledger before runtime isolation is implemented.

## Configure and check without network access

Copy `examples/live-endpoints.example.json` to a local deployment manifest. Replace all
example hosts, region/zone placeholders, model revisions and locality evidence with reviewed
values. No AWS-to-grid-zone mappings are inferred. Use the same model alias and immutable
revision across deployments; regional served model names may differ. Select a fixed baseline.
The checker validates declaration consistency; it cannot prove those declarations are true.

```sh
python scripts/check_live_config.py examples/live-endpoints.example.json
```

This command always performs zero network requests, including when environment credentials
are present. It reports only booleans for credential availability, never secret values,
URLs, provider responses or environment-variable contents. Placeholder/example manifests
are structurally valid but are not evidence of live readiness.

When access becomes available, put each regional vLLM key in its declared environment
variable and the grid key in `ELECTRICITYMAPS_API_KEY`. Do not put keys in JSON, URLs,
commits, screenshots or issue comments. The example contains environment variable **names** only.
The existing demo `.env` and `Settings` remain unchanged.

## Adapter contracts

`greenroute.live.VLLMAdapter` accepts administrator-supplied HTTPS `/v1` deployments.
`probe()` reads authenticated `/v1/models` and checks the exact served model ID. A successful
probe does not verify locality, GPU hardware, model revision or chat-template equivalence.
`execute()` maps the public alias to that deployment's served model and reuses the gateway's
nonstreaming completion validation, token usage parsing and uncertain-execution/retry contract.
Redirects are disabled, keys are resolved from the environment and no caller headers are forwarded.
There is no automatic inference retry, cross-region fallback or automatic billed probe.
Callers must create an `httpx.AsyncClient(trust_env=False)` and close it after use.

`ElectricityMapsAdapter` reads v4 `/carbon-intensity/latest` with explicit zones, lifecycle
emission factors, flow-traced consumption accounting and IP lookup disabled. It preserves
`datetime` as the observation time and marks observations `live`; `updatedAt` is not substituted.
Responses with mismatched zones/accounting, invalid values, future/naive/stale timestamps,
provider errors or redirects fail the whole batch with a sanitized error. No demo fallback
or hidden retry occurs. The caller owns polling/backoff/cache and credential provisioning.
The freshness default is 300 seconds; confirm purchased temporal granularity and availability
before choosing a reviewed freshness policy. Hourly signals may legitimately fail that limit.
The demo poller deliberately rejects live provenance; do not attach this adapter to it.

## Remaining gates before live runtime activation

1. Confirm API entitlement, rate limits, temporal granularity and authoritative zone mappings.
2. Review regional processing locality evidence, model revision/template equivalence and endpoint security.
3. Agree an inference/token/request budget and real experiment procedure.
4. Implement and test separate live registry/database, policy eligibility, cache partitioning,
   worker health checks, API provenance and dashboard labeling before enabling live mode.
5. Perform read-only model/grid probes, then an authorized bounded completion with actual
   provider IDs, usage and source timestamps. Exercise auth, rate-limit, outage and stale data paths.
6. Record the fixed-baseline comparison. Energy coefficients remain illustrative until calibrated;
   live grid data and token usage alone do not establish measured emissions or avoided carbon.

## Offline evidence

```sh
python -m pytest tests/test_live.py -q
python -m pytest -q
```

Tests use `httpx.MockTransport`, never real credentials or services. They cover source timestamps,
explicit provenance, stale/future/invalid data, HTTP failures, redirect refusal, configuration
constraints, alias mapping, regional authentication, missing credentials and retry semantics.
These are synthetic contract tests, not recorded provider responses or real feasibility evidence.

API contracts checked 2026-10-28 against primary documentation:
- [Electricity Maps v4 latest carbon intensity](https://app.electricitymaps.com/docs/reference/carbon-intensity/latest)
- [Electricity Maps authorization/reference](https://app.electricitymaps.com/docs)
- [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)
