# Isolated live-runtime verification

Verified 2026-10-28 using synthetic HTTP providers and disposable local services.
No real provider access, locality verification or energy calibration is claimed.

- Backend regression: **153 tests passed**, including **17 live-runtime integration tests**.
- Migration 004 → 005 preserves existing demo decision evidence and endpoint registry.
- Live registry initialization rejects databases with execution evidence and unreviewed manifests.
- Live chat, carbon-based worker dispatch, threshold waiting/release, stale-grid retention,
  401/429/5xx behavior, read-only circuit recovery and cross-mode cache/metrics isolation passed.
- Mode/manifest/registry mismatch blocks startup or dispatch; manifest changes cannot redirect
  active process traffic. Demo scenario controls/dashboard are unavailable in live mode.

The existing demo process/performance/fault harness also passed after these changes:
**32.521 ms p95 paired gateway overhead** across 1,000 pairs at concurrency ten,
and **16.08 seconds** to drain 500 jobs with nominal 100 ms mock latency and ten worker slots.
These meet the existing 100 ms / 300 second local targets. They do not predict regional provider
performance. See [machine-readable report](live-runtime-regression.json) for profile, timestamps,
methodology, host/dependencies and fault results. Earlier M5 reports remain historical evidence.

Repeat from the repository root:

```sh
python -m pytest -q
python scripts/run_verification.py --output runtime/live-runtime-regression.json
```

The full suite includes local loopback process tests; the live integration tests themselves use
`httpx.MockTransport`. The harness discards inherited provider configuration and uses its own
mock processes, credentials and databases. The live runtime activation procedure and remaining
real-trial gates are in [the integration guide](../guides/live-integration.md).
