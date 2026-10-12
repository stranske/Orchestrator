# Adjudicator cost evidence

Native CLI completion events carry input/output token usage through `dispatcher.offload`, `roles.run_adjudicator_agent`, and `feedback.record_role_run`. Assistant-deliverable JSON is not a usage receipt. Role metadata preserves the counts and missing-usage flag; the Brain token row is a `ledger` proxy and cannot satisfy complete-source dollar coverage. Record-only retries preserve richer receipts.

Shadow reports use the existing `list_price_usd` scale and existing coverage/plausibility eligibility. A complete native receipt is measured. Otherwise an eligible own-agent mean, then an eligible global upper median, is explicitly imputed. Without an eligible pool the report names UNMEASURED; it never fabricates a zero price. Estimates are read-only report values, never observations in the cost learner. Refreshing the report consumes late native dollars without another paid dispatch. Existing measured-only cost fields and the evidence floor retain their meaning.

The weekly/switch-review line reports `cost measured N of M, imputed K, unmeasured U`, an estimated accounted total, and its scale. That total only covers measured or eligible imputed rows; any remaining rows stay explicitly unmeasured. This does not recover missing historical receipts or establish whole-delivery cost acceptance for broader shadow experiments.

## Validation

```sh
python3 -m pytest -q tests/test_adjudicator_retro.py tests/test_adjudicator_retro_cost.py tests/test_offload_log_per_run.py
python3 -m mypy src/feedback.py src/roles.py src/dispatcher.py src/adjudicator_retro.py
python3 src/feedback.py --selftest
python3 src/roles.py --selftest
python3 src/dispatcher.py --selftest
python3 src/adjudicator_retro.py --selftest
```

The focused gate passed 76 cases, including 14 new regressions; the broader adjudicator/feedback/dispatch gate passed 214 cases and 23 subtests. Exact collection is 2922; the floor is the previous executed-node union plus these new cases, not a new full-suite verdict. Full hosted verification remains required.

Three independent production controls each fail their named test with an assertion (exit 1), then pass after byte-identical restoration (exit 0): omit role input tokens; remove weekly cost coverage wording; discard terminal usage before returning the reduced output. The named tests are `test_dispatch_records_measured_cost_when_the_adapter_reports_usage`, `test_missing_usage_is_imputed_and_flagged_never_null`, and `test_offload_preserves_native_terminal_usage_before_result_reduction` in `tests/test_adjudicator_retro_cost.py`. Raw synthetic logs, JUnit, and SHA256 restoration receipts are retained by the opener in its issue527 run evidence; no live Brain contents or paid sessions are part of this proof.

## PR545 review recovery

Terminal usage is accepted only when the requested adapter transport is a CLI event stream: Codex `--json` admits `turn.completed`; Cursor/Gemini `--output-format stream-json` admits `result`. Plain-text JSON is not a native receipt. Verified counts accumulate across every executed network retry, including a failed attempt that reports usage. Zero-dollar complete-source rows are not measured cost; they use an eligible imputation pool or remain unmeasured.

The metadata-only persistence regression uses a private Brain database explicitly, so an unavailable machine-local database cannot break a checkout test. Integrated collection is 2,937 nodes; all previous skip ceilings are preserved. The eight new review regression nodes exercise transport provenance, retry accumulation, zero-dollar handling, and Codex completion usage.

Current bounded proof is in `docs/verification/issue-527-review-recovery/`: prior production fails seven assertions; byte-identical candidate restoration passes all 84 focused tests. The native event streams here are synthetic subprocess fixtures, not newly purchased agent calls or recovered historical cost. Full local checkout verification independently executed 2,937 tests with zero failures/skips, all 104 module selftests and all five gates. The raw summary is retained in full-checkout-verifier.txt. Hosted checks and actual post-merge comparison remain required.
