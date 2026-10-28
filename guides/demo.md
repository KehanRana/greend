# GreenRoute demo and handoff

## Prepare

Follow the root README to install from both lockfiles, build the dashboard and migrate. Start mock, gateway and worker on the same environment/database. Configure distinct client/operator keys. Use a new database path for a presentation when you want isolated totals; keep any previous database rather than clearing it.

Confirm `/ready` is successful and the dashboard loads. Connect using the client key. Explain that this is a local, regional-grid-aware simulation: three locations, synthetic summaries/usage and an illustrative energy coefficient. It does not connect to a measured micro-grid or prove real avoided emissions.

## Four-minute walkthrough

1. **Immediate chat.** Run `python examples/interactive.py`. Show that a standard OpenAI SDK call returns a completion from the preferred healthy endpoint. Interactive requests do not wait for cleaner grid signals.
2. **Flexible work waits.** In the dashboard's Grid scenario dialog, apply High carbon with the operator key. Submit five jobs with threshold 150. Show waiting state, deadline and eligible region values; optionally cancel one before dispatch.
3. **Release clean work.** Apply Clean Frankfurt. Watch the worker select Frankfurt as the threshold becomes satisfied. The scenario changes future observations and leaves earlier work/totals intact.
4. **Inspect the estimate.** Open a succeeded decision. Point out selected/default endpoints, source timestamps, model coefficient/version, simulated usage and the signed difference. Missing evidence is excluded; the primary baseline is fixed, not chosen after the result.
5. **Scale the same path.** Run the command below for the full replay. Distinguish this-run state counts from workspace estimate totals. Show that the stored manifest can be resumed without adding duplicate jobs during the 24-hour mapping window.

```sh
python examples/demo_batch.py --count 500 --release-clean --run-file runtime/presentation-run.json
```

The script changes the workspace's shared grid scenario; other waiting jobs can also release. A new run needs a new filename. An existing run file determines its stored bodies/deadlines and job count. Keep the worker and gateway running while polling.

## Explain the numbers

At 56 synthetic tokens per response and 0.001 kWh per 1,000 tokens, 500 responses represent **0.028 estimated kWh**. With a 400 gCO2e/kWh default versus 80 gCO2e/kWh selected region, the spatial difference is **8.96 estimated grams**. Those are fixture arithmetic, not measured electricity or carbon reductions.

The baseline/selected estimate excludes failed-attempt energy, networking, cooling, embodied impacts and temporal shifting. Do not present the difference as offsets or certified reporting data. The location mapping and energy model need validation before live claims.

## Fallbacks

- Grid source unavailable: show the degraded/freshness state and explain why flexible work waits. Interactive chat still uses healthy endpoint order.
- No worker progress: verify the worker is running against the same database and inspect owner job detail. Do not create a new key/body just to repeat a timed-out submission.
- Unknown execution: show that uncertainty is retained instead of blindly replayed. There is no automatic provider reconciliation.
- Dashboard unavailable: use SDK/batch examples plus `/api/regions`, `/api/jobs/{id}` and `/api/metrics`; rebuilding/restarting the gateway restores its static mount.
- Presentation timing is short: use five jobs in the UI; use the recorded 500-job report as scoped local evidence, not as proof of live regional performance.

## Handoff inventory

- `README.md`: install, configuration, API surface and examples.
- `frontend/DESIGN.md`: visual direction, data provenance and responsive/accessibility review.
- `guides/architecture.md`: module ownership and transaction boundaries.
- `guides/operations.md`: failure diagnosis, safe recovery and retention.
- `verification/m5-report.json`: raw matched RTT pairs, drain result, hardware/dependencies and physical failure checks.
- `verification/rehearsal-report.json`: fresh environment/source-copy install/build/test/startup evidence.
- `verification/requirements-matrix.md`: requirements traced to evidence and explicit boundaries.

Next is conditional live integration: establish endpoint processing locality, equivalent model versions, grid API entitlement/zone mappings and an approved energy measurement model. No live-provider integration or production deployment is included in this handoff.
