# Issue 1026 repair evidence

Checked 2026-11-03 UTC against main `44cbb45e5d2bada21592cc6b83957ef6e3790030` plus both preserved #1028 commits (`ec43d9d`, `c2d55c0`). Live #1028 was OPEN, not merged. No `closer*/issue-1026` branch existed before this run. No original test was discarded.

## Outcome and stop rule

Outcome 1 is a ready repair PR with exact-head required CI passing, unique shard assignment, and recorded mutation failures/restored passes. Stop after two focused repair attempts or 30 minutes. Two focused repair attempts were used; no third repair is authorized by this run's stop rule. No local full test suite or verify pipeline was run.

Focused acceptance command (private fresh directories for both environment variables):

```sh
ORCH_STATE_DIR=<private-temp>/state ORCH_LOCAL_RUNTIME=<private-temp>/runtime PYTHONPATH=src /opt/anaconda3/bin/python3 -m pytest tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once tests/test_coverage_round_877.py tests/test_coverage_round_1026.py -q
```

`validation.txt` records the baseline failure and restored 24-pass outcome. The partition test itself remains unchanged. Black `--check --line-length 100` and Ruff check passed on both test files. External process, socket connect, and DNS attempts are denied and counted by fixtures, including caught errors; the final focused run recorded no attempts.

`mutations.json` records each named node, production function, temporary early-return fault, exit statuses, and restored production SHA-256. `mutation-output.txt` records the actual failed assertion/node and restored-pass output for all 19 added tests and four pre-existing tests. These faults establish sensitivity to selected wrong behaviors, not exhaustive branch-mutation adequacy. Every production edit was restored in a finally block; no production change is in the repair.

## Dispatcher attribution

Historical CI run 41554962180 failed three cases in `tests/test_closer_dispatcher.py`: the two parameterizations of `test_cli_pipeline_budget_does_not_renew_or_inherit_stale_legacy_accounting` and `test_expired_admission_reports_drainable_zero_as_deadlock`. All graded `unmeasured` instead of pass/fail. Main commit `ccb3cc42eca14ae33c918e3ca66fe45562532912` (#1056) pins `PIPELINE_ADMISSION_PASS_CHECK_TS` ahead of each test's clock. All three now pass in 0.44 seconds on the same main base used for this repair. Neither dispatcher source nor its tests were changed here.

## Ranking and combined coverage uncertainty

Issue #1026 supplies the ranking at main `21b8ad24`, measured 2026-10-29T22:15:13Z: capability_admission (6 escaped-defect signals, churn 14, 86 uncovered units), env_prereq (6, 12, 45), ledger_reconcile (6, 10, 74). This repair preserves that order and scope.

Historical focused pytest-only percentages reported in #1028 were 46.3 -> 63.2, 21.1 -> 40.5, and 25.0 -> 54.9 respectively; they are not combined deltas and were not remeasured here. Historical CI run 41554962180 published no combined coverage payload. Until the repair workflow completes, zero of the historical 205 target units have a confirmed combined before/after measurement; the entire 180-unit requested gain remains unverified. The historical-scope gain could be 0 through 205 units (shortfall against 180: 0 through 180). This interval does not bound current-main file growth or regressions. env_prereq's 45-unit historical count has no stable line list. No positive combined gain is claimed.

The existing CI workflow runs `src/verify.py --coverage` through its shard/aggregate path. Its exact-head result, rather than a local full run, determines CI completion and any available combined measurement.

## Attempt 2: flat-layout failure

CI run 41678655576 on `3b31258009e9b93f78c8933e90af8b749d19a142` passed all six normal shards but flat shard 2 failed the harness test's assumption that `src/env_prereq.py` exists. The test now stubs `paths.checkout_root` to its private temporary directory and creates `present.txt`, checking real presence/absence behavior in either layout. No production edit remains. `attempt-2/` records a second behavioral mutation of `repo_files_absent` (returning None for a missing file): the exact harness node failed, then passed after restoration. Final named acceptance command: 24 passed in 10.07s. Black and Ruff passed on the changed test file. CI on the second head remains the required acceptance step.
