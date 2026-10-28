# Requirements-to-evidence matrix

Scope: local simulated MVP. “Verified” means the observable demo behavior passed, not that a live regional provider or production deployment was certified. Performance data is in [m5-report.json](m5-report.json); setup evidence is in [rehearsal-report.json](rehearsal-report.json). The engineering baseline remains `docs/requirements.md` (locally retained).

| ID | Evidence | Result / boundary |
|---|---|---|
| FR-01 | `tests/test_slice.py::test_auth_limits_headers_fallback`; `test_jobs.py::test_same_key_is_scoped_by_owner` | Verified client authentication, separate upstream key and owner-scoped output |
| FR-02 | `test_slice.py::test_validation`, `test_job_policy_schema` | Verified supported text/non-streaming subset and rejection of unsupported fields |
| FR-03 | `test_slice.py::test_sdk_and_restart`, `test_upstream_errors`; verification smoke official SDK | Verified parsed SDK completion and structured errors |
| FR-04 | `test_jobs.py::test_api_idempotency_deadline_and_owner_isolation`; verification smoke | Verified durable 202 job API separate from SDK completion route |
| FR-05 | `test_jobs.py::test_atomic_queue_capacity_and_duplicate_submissions`, `test_same_key_is_scoped_by_owner`, `test_retention_active_payloads_and_key_reuse`; replay/resume process test | Verified owner/key/body identity, conflict and retention semantics |
| FR-06 | `test_grid.py::test_fixture_drop_and_durable_cache`, `test_invalid_values_are_persisted_as_unusable`; grid outage case | Verified timestamped fixture adapter/cache; actual grid entitlement remains unverified |
| FR-07 | `test_slice.py::test_auth_limits_headers_fallback`; `test_grid.py::test_regions_api_and_interactive_independence`; physical grid outage case | Verified immediate rank selection/fallback independent of carbon source |
| FR-08 | `test_jobs.py::test_cancel_claim_race_and_competing_claims`, `test_global_worker_limit_and_leases`; API restart and SIGKILL cases | Verified acknowledgment durability, atomic claims and conservative recovery |
| FR-09 | `test_jobs.py::test_admission_deadlines_are_separate_from_persisted_policy`, `test_worker_deadline_boundary`; pure policy boundary tests | Verified new-admission deadline validation and stored policy handling |
| FR-10 | `test_carbon_policy.py::test_endpoint_eligibility_before_carbon`, `test_allowed_regions_cannot_be_bypassed`, `test_ties_use_latency_then_stable_id_independent_of_input_order` | Verified in simulated locality; real locality/model equivalence remains a live gate |
| FR-11 | `test_estimation.py::test_known_arithmetic_and_secondary_comparison`, `test_dispatch_assumptions_are_immutable_and_deduplicated`, `test_aggregates_separate_modes_versions_exclusions_and_zero` | Verified arithmetic and evidence; energy/usage remains illustrative/simulated |
| FR-12 | `test_grid.py::test_regions_api_and_interactive_independence`; `test_estimation.py::test_metrics_auth_empty_and_synchronous_persistence`; frontend stale-state test | Verified content-free metadata and empty/stale/degraded/error handling |
| FR-13 | M4 production screenshots and seven-point review in `frontend/DESIGN.md`; frontend API-derived totals/cancellation tests | Verified overview map, textual values, source/evidence, deadlines and signed estimates |
| FR-14 | `test_estimation.py::test_scenario_auth_release_without_reset_and_restart`, `test_live_mode_has_no_scenario_mutations`; 500-job benchmark | Verified persistent operator controls and deterministic synthetic replay; live mode unsupported |
| FR-15 | `test_jobs.py::test_api_idempotency_deadline_and_owner_isolation`, `test_cancel_claim_race_and_competing_claims`, `test_http_date_retry_guidance_and_cancel_pending_retry` | Verified polling/output ownership and cancellation before dispatch |
| FR-16 | `test_jobs.py::test_atomic_queue_capacity_and_duplicate_submissions`, `test_global_worker_limit_and_leases`, `test_queue_full_response_has_retry_guidance`, `test_interactive_capacity_rejects_without_upstream_call` | Verified configured caps, atomic capacity and 429 guidance |
| FR-17 | `test_jobs.py::test_safe_retry_limit_and_backoff`, `test_adapter_failure_classification`, `test_retry_after_is_honored_and_never_retried_past_deadline`; physical 429/provider outage/SIGKILL cases | Verified bounded safe retries and unknown-state preservation; no exactly-once claim |
| FR-18 | No CSV export | Optional Should requirement; deferred |
| NFR-01 | Standard matched-pair benchmark: 1,000 gateway calls, ten lanes, 100 ms nominal local upstream, 30 warm-up pairs excluded | Passed: 28.784 ms p95 paired overhead ≤100 ms. Approximate RTT subtraction, not production tail-latency isolation |
| NFR-02 | Standard 500-job admission/wait/release/drain benchmark, ten worker slots, default ten-second poll | Passed: 500 unique jobs / 500 attempts completed in 16.172 s after release ≤300 s |
| NFR-03 | API restart smoke; competing claim/cancel tests; physical worker SIGKILL after provider receipt | Verified durable state, stale-owner rejection and unknown without replay |
| NFR-04 | `test_grid.py::test_retrieval_does_not_rejuvenate_old_source_time`, `test_signal_quality_boundaries`, `test_failure_backoff_and_recovery_keep_cache`; physical shortened-TTL outage | Verified production TTL/poll logic in clock tests and fault behavior with explicit test-only timings |
| NFR-05 | Config/auth/header/permission tests, owner/operator tests, sanitized provider/storage errors | Local controls verified. Documented loopback binding; remote TLS/production security not deployed or verified |
| NFR-06 | `test_jobs.py::test_retention_active_payloads_and_key_reuse`, `test_expired_result_is_hidden_before_cleanup_and_success_is_immutable`; database permission test | Verified logical deletion/access controls; secure erase and deployment encryption remain external responsibilities |
| NFR-07 | Correlated job/attempt/decision APIs; health/readiness, region freshness, owner queue and outcome counts | Verified metadata surfaces. No production monitoring/alerting or worker-liveness SLA claimed |
| NFR-08 | M4 1440/768/390 inspection: zero overflow/findings; axe tags: zero detected violations; frontend dialog/menu focus tests | Scoped checks passed; not full WCAG certification of every state |
| NFR-09 | Pure policy fixtures plus module/transaction review in `guides/architecture.md` | Verified deterministic policy separation; architectural review is manual evidence |
| NFR-10 | Fresh copied source, new venv, pip lock install/check, npm ci/build, full tests and process smoke | See clean rehearsal result; no external provider/grid keys or preexisting database used |
| NFR-11 | Grid fetch timeout/backoff tests; provider failure classifier and bounded retry tests; frontend last-good-data test | Verified provider/grid timeout and retry boundaries; frontend resource requests can be aborted on refresh/disconnect |
| NFR-12 | Full Python/frontend suites, this matrix, separate performance and process-fault report | Verified coverage of Must behaviors within demo scope; production/live limits remain explicit |

## How to interpret the performance report

The latency runner alternates direct/gateway request order within ten lanes. It computes nearest-rank p95 from each pair's gateway RTT minus direct RTT, rather than subtracting the two marginal p95 values. Jitter remains, negative differences are preserved and raw pairs are retained. Both phases run on one Apple M4 host; regional network travel and real model computation are not represented.

Batch drain starts immediately before the operator release request and includes its refresh, worker scheduling, completion writes and up to 250 ms polling detection delay. Admission is measured separately. A SQLite abort trigger represents admission write failure; it is not a physical disk-full test. Test-only grid fault timing is accelerated and disclosed in the JSON case evidence.
