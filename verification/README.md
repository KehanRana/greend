# M5 verification results

Recorded 28 October 2026 on Apple M4 / 10 logical CPUs, macOS arm64, Python 3.14.5. These are scoped local measurements, not a production performance promise.

| Check | Required target | Observed | Result |
|---|---|---|---|
| Matched proxy overhead | p95 ≤100 ms; 1,000 calls; concurrency 10 | **28.784 ms** p95 per-pair RTT difference | Passed |
| Deferred batch drain | 500 jobs; 100 ms mock; ten worker slots; ≤300 s after release | **16.172 s**, 500 jobs / 500 attempts / zero lost | Passed |
| Admission | Persist all acknowledged jobs | 500 unique 202 responses in 0.641 s | Passed |
| Fresh setup | New environment from lockfiles; build/tests/startup | **106 Python + 7 UI tests**, build and real-process smoke | Passed |
| Physical failures | Retain uncertainty, bounded retry and recoverability | Five process/storage/grid scenarios passed | Passed |

Raw [performance/failure report](m5-report.json) includes all 1,000 matched pairs and hardware/dependency details. The [fresh rehearsal report](rehearsal-report.json) records each install/build/test step and both lockfile hashes. The [requirements matrix](requirements-matrix.md) traces every Must behavior and marks optional/live/production limits.

## Repeat

With dependencies installed and the dashboard built:

```sh
python scripts/run_verification.py
```

The runner creates its own temporary database, generated credentials and loopback services, ignores inherited `GREENROUTE_*` configuration, and shuts down only processes it spawned. It never reads your `.env` or sends inference/grid calls externally. The normal mock gains a configurable synthetic delay; test-only failure control endpoints exist only in the verification service factories.

For a new source copy, fresh venv, pinned installs, full tests and service smoke:

```sh
python scripts/rehearse.py
```

The rehearsal copies application/tests/examples and lockfiles, not `.env`, databases, existing venv or node_modules. Dependencies can use package-manager caches, but the environments/source directory are new. Registry access is needed when packages are unavailable locally. This is a source-copy rehearsal, not a Git-clone or cross-platform certification.

Both commands overwrite their respective JSON reports by default. Use `--output runtime/my-report.json` to keep checked-in evidence unchanged. `--smoke-only` on the verification runner skips benchmarks/fault cases and must not be presented as a performance pass. Nonstandard benchmark sizes do not pass the standard target gate.

## Method and interpretation

The latency phase alternates pair order across ten lanes, with one in-flight request per lane. It measures client-observed direct and gateway RTTs against a nominal 100 ms local mock and computes nearest-rank p95 of the **individual differences**. Thirty warm-up pairs are excluded. Negative differences would remain in the data. The direct median was 102.775 ms; the gateway median was 117.636 ms. Jitter is not perfectly cancelled, and subtracting marginal p95 values would give a different statistic.

The batch phase verifies all 500 jobs are waiting above threshold before applying clean Frankfurt. Drain timing starts before that operator request, includes refresh/scheduling/completion writes, and has up to 250 ms polling detection delay. Worker concurrency ten and polling ten seconds are the normal defaults. Each job has one successful attempt. The 8.96 g comparison is fixture arithmetic under an illustrative coefficient, not measured avoided emissions.

Failure checks use a separate accelerated test configuration: one-second inference timeout, two-second lease, 100 ms worker polling/retry base, and a 1.5-second grid freshness limit. They kill a worker after the provider confirms receipt, kill/restart the provider, inject 429, reject admission writes with a SQLite abort trigger and interrupt the synthetic grid source. They do not fill a disk, execute a real GPU job or certify an external grid API.

UI visual/accessibility evidence is carried forward from M4 because M5 changes no frontend behavior. M4 inspected the production build at 1440/768/390 with no overflow/style findings and no detected axe WCAG-tagged violations, plus manual keyboard flows. These checks are not full accessibility certification.

## Boundaries

No live provider locality, API entitlement, calibrated energy model, remote TLS deployment, production availability, multi-host behavior or long-history scaling was verified. Metadata is shared among clients, while job output is owner-scoped. Exactly-once provider execution is not claimed; unknown records are retained rather than replayed.
