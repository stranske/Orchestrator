# Issue #1102 — capability_advisor Gate coverage evidence

Recorded before any edit to `src/capability_advisor.py` on 2026-11-05. This report
uses the recovered Gate `coverage.json` artifacts. It does not mix them with PR #650's
local cohort.

## Real-input acceptance

- Check date: 2026-11-05
- Python: Python 3.14.9 (/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3)
- /opt/anaconda3/bin/python3 is absent on this runner
- Attempts: 1
- Elapsed: 0.64 seconds
- Stop rule: Stop after 2 attempts or 120 seconds elapsed, whichever comes first. Do not start an attempt after 90 seconds have already elapsed.
- Ledger: temporary ORCH_LOCAL_RUNTIME with no capabilities.json; load_declared therefore saw no rows
- Command: set `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`, and `ORCH_CAPABILITIES_PATH`
  to a fresh temporary directory, set `PYTHONPATH=src`, and run
  `python3 -c` with `capability_advisor.advise('add unit tests for the retry helper',
  repository='', record=False)`, printing `useful`, `confidence`, `task_types`,
  `capability_ids`, `dispatch_ready_count`, and `reason`.
- Outcome: `{"capability_ids": [], "confidence": "high", "dispatch_ready_count": 0, "reason": "classified as testgen, but no capability declares a trigger for that work", "task_types": ["testgen"], "useful": false}`

## Revisions and ranking

- Ranking source: https://github.com/stranske/Orchestrator/issues/647 (2026-10-16T21:09:09Z)
- Order: escaped defects, then churn, then uncovered mass
- Selected: `src/capability_advisor.py` rank 1, 68 reported uncovered units, 22 escaped-defect signals, churn 45 (fix commit touched this file)
- Left alone: src/adversarial.py, src/capabilities.py, src/capability_admission.py, src/runtime_ac.py
- A git recount over 2026-04-19..2026-10-16 on this path is 24 fix/revert touches and churn 46. It is not the selection record.
- PR #650 tests at head: `tests/test_capability_advisor_coverage.py` (13 tests).
- Base `ec9c7e09aaca054dac1d38151e766827b0658c43`: parent of PR #650's first commit aa0172d.
- Merge base `143ce07ceacae5f4d973219f1267045133825aa7`: main merged into the PR before the final commit; advisor slice matches base.
- Head `d477734a1d2ee3bb93daa599e6bb52b64c308678`: PR #650 head d477734, CI run whose gate-coverage artifact was recovered.
- Squash merge onto main: `d6e0d40057c60b19481c39f7f85015ad5c6c8731`.

## Measurement configuration

- Gate command: `python3 src/verify.py --coverage`
- CI shard command: `python3 src/verify.py --pytest-shard <n>/6 --coverage --shard-output shard-results`
- CI aggregate command: `python3 src/verify.py --aggregate shard-evidence --coverage`
- Artifact name: `gate-coverage` file `coverage.json`
- coverage.py 7.16.2, branch_coverage=False, source=['src'], parallel=True, relative_files=True, config `pyproject.toml` `[tool.coverage.run]` branch=False

## Comparable Gate results for src/capability_advisor.py

| Revision | CI run | Statements | Covered | Uncovered statements | Uncovered branches |
| --- | ---: | ---: | ---: | ---: | --- |
| `ec9c7e09aaca` | 38970976941 | 1593 | 1525 | 68 | unmeasured |
| `143ce07ceaca` | 38972960240 | 1593 | 1525 | 68 | unmeasured |
| `d477734a1d2e` | 38977286469 | 1602 | 1563 | 39 | unmeasured |

- Base artifact: https://github.com/stranske/Orchestrator/actions/runs/38970976941
- Merge-base artifact: https://github.com/stranske/Orchestrator/actions/runs/38972960240
- Head artifact: https://github.com/stranske/Orchestrator/actions/runs/38977286469
- Missing statements: 68 at base and at merge base, 39 at head.
- Statement reduction, base to head: 29.
- Statement reduction, merge base to head: 29.
- Uncovered branch locations: none. `meta.branch_coverage` is false, so branches were
  not measured. That is not a measured zero and it is not part of the reduction.
- Uncovered statement locations at base: [201, 202, 261, 521, 533, 534, 700, 701, 935, 938, 1684, 2125, 2133, 2186, 2187, 2206, 2207, 2211, 2225, 2226, 2228, 2493, 2537, 2538, 2551, 2552, 2682, 2693, 2697, 2698, 2703, 2790, 2801, 2802, 3013, 3014, 3030, 3031, 3815, 3913, 3940, 3941, 3948, 3949, 3950, 3951, 3952, 3954, 3955, 3957, 3958, 3959, 3960, 4208, 4545, 4606, 5115, 5252, 5261, 5545, 5549, 5550, 5551, 5552, 5553, 5554, 5568, 5569]
- Uncovered statement locations at head: [201, 202, 533, 534, 700, 701, 935, 938, 1684, 2206, 2207, 2211, 2228, 2703, 2790, 3013, 3014, 3815, 3913, 3940, 3941, 3948, 3949, 3950, 3951, 3952, 3954, 3955, 3957, 3958, 3959, 3960, 4208, 4545, 4606, 5115, 5252, 5261, 5578]

## Target

Issue #647 asked for about 60 fewer uncovered units. The comparable Gate statement reduction is 29. The shortfall on that measure is 31 statements. No branch reduction is claimed.

PR #650's local cohort went from 94 missing (1499/1593) to 61 missing (1532/1593), a drop of 33. The Gate base is 1525/1593 with 68 missing, so the local denominator is a different measurement. It is not added to the Gate reduction and it is not the 68-unit baseline.

Coverage delivered by #650, on the Gate statement measure: 29. Coverage added by this
successor is not in these artifacts and is not included in that 29.

The statements still missing at head, and why #650's tests did not execute them:

- `src/capability_advisor.py:201` — _dispatcher_task_type_capability import except. The except returns only when the import itself fails. PR #650 patches the helper after the import has succeeded, so this statement does not run.
- `src/capability_advisor.py:202` — return {} on that import failure. The except returns only when the import itself fails. PR #650 patches the helper after the import has succeeded, so this statement does not run.
- `src/capability_advisor.py:533` — _record_fact_missing import except. The except returns only when the import itself fails. PR #650 patches the helper after the import has succeeded, so this statement does not run.
- `src/capability_advisor.py:534` — return 0 on that import failure. The except returns only when the import itself fails. PR #650 patches the helper after the import has succeeded, so this statement does not run.
- `src/capability_advisor.py:700` — classification-miss path. advise swallows a capability_propensity.rank failure. PR #650's consult tests replace advise, so this except does not run.
- `src/capability_advisor.py:701` — pass on that rank failure. advise swallows a capability_propensity.rank failure. PR #650's consult tests replace advise, so this except does not run.
- `src/capability_advisor.py:935` — classified path. advise swallows a capability_propensity.rank failure. PR #650's consult tests replace advise, so this except does not run.
- `src/capability_advisor.py:938` — pass on that rank failure. The lines between the except and this pass are comments. advise swallows a capability_propensity.rank failure. PR #650's consult tests replace advise, so this except does not run.
- `src/capability_advisor.py:1684` — _promoted_index writes a binding_promotion event. PR #650 never passes a ledger event whose metadata source is binding_promotion.
- `src/capability_advisor.py:2206` — consult_phases_guarded finally-handler except (ValueError, OSError). PR #650 makes signal.signal raise before the alarm is armed, so this except is skipped.
- `src/capability_advisor.py:2207` — pass on that cleanup failure. Same unentered except as line 2206.
- `src/capability_advisor.py:2211` — _consult_expired raises TimeoutError. PR #650 never calls the SIGALRM handler.
- `src/capability_advisor.py:2228` — format_phase_consult success-row branch. PR #650 renders phase rows that carry an error, which takes the other branch.
- `src/capability_advisor.py:2703` — detect_observable_surface returns once the marker count reaches _SURFACE_MAX_MARKERS (6). PR #650's scan finds three markers.
- `src/capability_advisor.py:2790` — evaluate_precondition when the required repo fact has no registered probe. PR #650 registers an exploding probe, which is the failure branch.
- `src/capability_advisor.py:3013` — _record_auto_declines import except. The except returns only when the import itself fails. PR #650 patches the helper after the import has succeeded, so this statement does not run.
- `src/capability_advisor.py:3014` — return 0 on that import failure. The except returns only when the import itself fails. PR #650 patches the helper after the import has succeeded, so this statement does not run.
- `src/capability_advisor.py:3815` — restore SURFACE_BINDINGS['t-howto']. This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does not pre-seed that key, so the selftest takes the pop branch.
- `src/capability_advisor.py:3913` — restore SURFACE_BINDINGS['t-concept']. This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does not pre-seed that key, so the selftest takes the pop branch.
- `src/capability_advisor.py:3940` — Line 3940 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3941` — Line 3941 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3948` — Line 3948 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3949` — Line 3949 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3950` — Line 3950 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3951` — Line 3951 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3952` — Line 3952 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3954` — Line 3954 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3955` — Line 3955 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3957` — Line 3957 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3958` — Line 3958 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3959` — Line 3959 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:3960` — Line 3960 of the front-door cases. This statement is inside _selftest_front_door, after the ledger-row gate. CI's bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so ledger_rows_absent returns first. PR #650 does not seed those rows.
- `src/capability_advisor.py:4208` — restore SURFACE_BINDINGS['t-contra']. This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does not pre-seed that key, so the selftest takes the pop branch.
- `src/capability_advisor.py:4545` — restore SURFACE_BINDINGS['t-empty']. This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does not pre-seed that key, so the selftest takes the pop branch.
- `src/capability_advisor.py:4606` — restore SURFACE_BINDINGS['t-surface']. This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does not pre-seed that key, so the selftest takes the pop branch.
- `src/capability_advisor.py:5115` — restore SURFACE_BINDINGS['t-precond']. This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does not pre-seed that key, so the selftest takes the pop branch.
- `src/capability_advisor.py:5252` — FIX: assert over KNOWN_UNCONSULTED. This statement is the body of the KNOWN_UNCONSULTED loop. At d477734 that table is intentionally empty, so the loop body does not run. A temporary entry also fails the selftest's stale-table assert unless the surface is bound and unconsulted.
- `src/capability_advisor.py:5261` — --surface contradiction assert. This statement is the body of the KNOWN_UNCONSULTED loop. At d477734 that table is intentionally empty, so the loop body does not run. A temporary entry also fails the selftest's stale-table assert unless the surface is bound and unconsulted.
- `src/capability_advisor.py:5578` — main calls argparse's error when argv has no task. PR #650's CLI test always passes a task or --consult-tick-phases.

## Deliberate breaks

Each break was applied only in `tests/issue1102_coverage_evidence.py`, then the file was restored to its original bytes. The command for every row was:

`python3 -m pytest -q tests/test_issue1102_advisor_coverage_evidence.py -m "not slow" --tb=line -k <test>`

| Test | Break | Failing result | Restored result |
| --- | --- | --- | --- |
| `test_unmeasured_branches_are_not_counted_as_zero` | treat unmeasured branches as zero | exit 1, `assert 0 is None` | exit 0 |
| `test_measured_branches_stay_separate_from_statements` | drop branch locations from the unit total | exit 1, `assert 2 == 4` | exit 0 |
| `test_pr650_gate_slices_show_a_29_statement_reduction` | add one to the Gate statement reduction | exit 1, `assert 30 == 29` | exit 0 |
| `test_local_94_to_61_is_not_a_gate_reduction` | mark the local 94/61 cohort comparable | exit 1, `assert True is False` | exit 0 |
| `test_issue_647_ranking_selected_only_capability_advisor` | select `src/other.py` | exit 1, `assert ['src/other.py'] == ['src/capability_advisor.py']` | exit 0 |
| `test_every_remaining_head_statement_has_its_own_limit` | replace a head-statement limit with `"short"` | exit 1, `assert False` | exit 0 |
| `test_real_input_acceptance_matches_the_recorded_outcome_within_two_minutes` | record `useful=true` for the empty-ledger call | exit 1, outcome dict mismatch | exit 0 |
| `test_committed_report_matches_the_renderer_and_names_the_artifacts` | call unmeasured branches a measured zero in the report | exit 1, rendered report no longer matches this file | exit 0 |

## Successor path tests

These tests are in `tests/test_capability_advisor_coverage.py`. They execute the Gate-head
statements a caller can reach. They are not a new `coverage.json`, so they are not added to
the Gate reduction of 29. Issue #647 ranked `src/capability_advisor.py` first; the paths below
are the head statements that ranking left uncovered.

Reached by this successor, named by the behavior in the head report (some lines moved after
`d477734`):

- Dispatcher import failure: head 201-202, current 201-202. `test_dispatcher_import_failure_degrades_direct_entry`
- Fact-missing import failure: head 533-534, current 533-534. `test_propensity_import_failure_records_nothing`
- Auto-decline import failure: head 3013-3014, current 3058-3059. Same test.
- Classification-miss rank failure: head 700-701, current 700-701. `test_classification_miss_survives_unreadable_propensity`
- Classified rank failure: head 935 and 938, current 935 and 938. `test_classified_advice_survives_unreadable_propensity`
- `binding_promotion` index write: head 1684, current 1719. `test_promoted_index_keeps_binding_promotion_events`
- Guarded-consult cleanup failure: head 2206-2207, current 2248-2249. `test_guarded_consult_swallows_alarm_cleanup_failure`
- SIGALRM handler: head 2211, current 2253. `test_consult_expiry_handler_raises_timeout`
- Successful phase row: head 2228, current 2270. `test_format_phase_consult_renders_a_successful_phase`
- Marker cap: head 2703, current 2748. `test_observable_surface_stops_once_six_markers_are_found`
- Unregistered repo-fact probe: head 2790, current 2835. `test_unregistered_repo_fact_probe_stays_unevaluated`
- CLI with no task: head 5578, current 5623. `test_cli_without_a_task_exits_with_the_three_ways_in`

Remaining head statements that this successor does not execute. Each limit is the current line.
`test_selftest_only_paths_stay_recorded_as_limits` pins the token on that line.

- `src/capability_advisor.py:3860` — else restores a pre-seeded t-howto entry; the selftest pops the key when it was absent, and production never pre-seeds that key
- `src/capability_advisor.py:3958` — else restores a pre-seeded t-concept entry; same pop-versus-restore split as t-howto
- `src/capability_advisor.py:4253` — else restores a pre-seeded t-contra entry inside _selftest_contraindications
- `src/capability_advisor.py:4590` — else restores a pre-seeded t-empty entry inside _selftest_bindings
- `src/capability_advisor.py:4651` — else restores a pre-seeded t-surface entry inside _selftest_bindings
- `src/capability_advisor.py:5160` — else restores a pre-seeded t-precond entry inside _selftest_preconditions
- `src/capability_advisor.py:3993` — _selftest_front_door returns at the ledger-row gate unless offload, codemod-campaign, and testgen-lane are already registered; those rows are machine-local, and a stand-in ledger would not satisfy the real matcher assertions
- `src/capability_advisor.py:5296` — the loop body is the tail of _selftest_findability, after that function has checked every consult-site caller; a copied assert would not execute these lines, and running the selftest section is the module selftest rather than a focused path test

## Deliberate breaks for the successor path tests

Each break was applied only in `tests/test_capability_advisor_coverage.py`, then the file was restored to its original bytes. The command for every row was:

`python3 -m pytest -q tests/test_capability_advisor_coverage.py -m "not slow" --tb=line -k <test>`

Checked 2026-11-05. Every broken run exited 1. Every restored rerun exited 0.

| Test | Break | Failing result | Restored result |
| --- | --- | --- | --- |
| `test_dispatcher_import_failure_degrades_direct_entry` | expect no import attempts | exit 1, `assert [] == ['dispatcher', 'dispatcher']` | exit 0 |
| `test_propensity_import_failure_records_nothing` | expect no import attempts | exit 1, `assert [] == ['capability_propensity', 'capability_propensity']` | exit 0 |
| `test_classification_miss_survives_unreadable_propensity` | expect confidence `high` | exit 1, `assert 'binding_only' == 'high'` | exit 0 |
| `test_classified_advice_survives_unreadable_propensity` | expect `useful` true | exit 1, `assert False is True` | exit 0 |
| `test_promoted_index_keeps_binding_promotion_events` | expect reason `unused` | exit 1, index diff `'used by hand'` | exit 0 |
| `test_guarded_consult_swallows_alarm_cleanup_failure` | expect no alarm calls | exit 1, `assert [] == [35, 0]` | exit 0 |
| `test_consult_expiry_handler_raises_timeout` | expect message `not the timeout` | exit 1, regex did not match `capability phase consult exceeded 30s` | exit 0 |
| `test_format_phase_consult_renders_a_successful_phase` | require `FAILED` in a success row | exit 1, `assert 'FAILED' in` the success line | exit 0 |
| `test_observable_surface_stops_once_six_markers_are_found` | expect 1 marker | exit 1, `assert 6 == 1` | exit 0 |
| `test_unregistered_repo_fact_probe_stays_unevaluated` | expect `requirement_met` false | exit 1, `assert None is False` | exit 0 |
| `test_cli_without_a_task_exits_with_the_three_ways_in` | expect exit code 0 | exit 1, `assert 2 == 0` | exit 0 |
| `test_selftest_only_paths_stay_recorded_as_limits` | point the t-howto limit at line 9999 | exit 1, `IndexError: list index out of range` | exit 0 |
