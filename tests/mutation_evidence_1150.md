# Issue 1150 repair acceptance

Check date: 2026-11-05.

Outcome 1: all 36 collected node IDs each reject one named, behavior-specific
temporary production mutation with the intended assertion failure, then pass
individually after byte-for-byte restoration. Production has no committed diff.

Command: `/opt/anaconda3/bin/python3 tests/mutation_runner_1150.py`.
Focused command: `/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue1150_ranked_coverage.py`.

Stop rule: one mutation/restoration attempt per case, at most 120 seconds for
each pair (55 seconds per pytest process); stop the batch on any unexpected
failure, surviving mutation, infrastructure error, timeout, or restoration error.
The batch is finite: 36 cases, with a 75-minute overall work limit. On a stop,
report the first broken step and ask whether to stop. No broader tests locally.

Blocking/drainable pair: an unverified mutation blocks the evidence claim;
the named isolated pair can run and restore source while that claim is blocked.

Earlier branch reconciliation: merged `closer-r1/issue-1150` commit `9b6299a8`.
Kept its saved `os.replace`, restoration, and recording-stub assertion. Replaced
the pass-only runner and unsupported blanket mutation/CI claims with executable
cases and fresh receipts. The original merged #1161 tests remain intact.


Observed final result: **36/36** intended assertion failures followed by **36/36**
restored passing results. Longest pair: **8.502s**; sum of pairs: **272.294s**.
Production bytes were checked after every pair and again after the complete batch.
The final test module, runner, and case-map SHA-256 hashes are in the receipt.

- [Exact case map](mutation_cases_1150.json): node ID, production function,
  exact before/after text, intended assertion and line, and behavior.
- [Raw receipts](evidence_1150/results.json): full unified mutation diff, original
  source hash, literal pytest commands, exit codes, raw stdout/stderr, JUnit
  assertion failure and restored pass, duration, and byte-restoration result.
- [Focused module output](evidence_1150/focused.txt).

Run from the repository root with the selected repository Python (this checkout
has no selected virtual environment, so the recorded interpreter is
`/opt/anaconda3/bin/python3`):

```sh
/opt/anaconda3/bin/python3 tests/mutation_runner_1150.py --output /tmp/issue1150-results.json
/opt/anaconda3/bin/python3 tests/mutation_runner_1150.py --case 08 --output /tmp/issue1150-case08.json
/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue1150_ranked_coverage.py
```

Each case command temporarily patches **only** the named production function,
then invokes the exact node shown below, once broken and once restored. Source
restoration is in `finally`, including when pytest errors or times out. Each
pytest process is limited to 55 seconds; each complete pair must finish under
120 seconds. Fresh, isolated Python bytecode paths prevent cached source reuse.
A red result counts only for exit 1, exactly one matching test case, no JUnit
errors/skips, and `AssertionError` at the intended test file and line. Collection,
import, setup, teardown, wrong-node and wrong-assertion failures cannot pass.

The changed test is case **08**. Its mutation skips `record_completion_event` and
returns the expected error anyway. The earlier error checks pass; the new
`assert recording_called` fails with `assert []`. Restoration passes, proving
the completion-recording scenario is independently reached after restoring
`os.replace`.

One mutation per test establishes sensitivity to the mapped behavior; it does
not claim exhaustive mutation coverage of every branch inside that test.

| Case (`--case`) | Exact node ID | Temporary production target | Assertion line | Broken/restored | Pair seconds |
| --- | --- | --- | --- | --- | --- |
| 01 | `tests/test_issue1150_ranked_coverage.py::test_capability_effect_rejects_fields_outside_the_contract` | `src/completion_event_adapter.py::validate_capability_effect_record` | 127 | exit 1 / exit 0 | 7.859 |
| 02 | `tests/test_issue1150_ranked_coverage.py::test_adapter_rejects_missing_event_field_repo_mismatch_arm_and_attempt_join` | `src/completion_event_adapter.py::adapt_completion_event_envelope` | 136 | exit 1 / exit 0 | 7.400 |
| 03 | `tests/test_issue1150_ranked_coverage.py::test_shape_readiness_reports_unreadable_ledger_and_cramped_weeks` | `src/adversarial.py::shape_shadow_readiness` | 172 | exit 1 / exit 0 | 7.483 |
| 04 | `tests/test_issue1150_ranked_coverage.py::test_shape_measurement_refuses_counts_above_the_population` | `src/adversarial.py::record_shape_measurement` | 196 | exit 1 / exit 0 | 7.435 |
| 05 | `tests/test_issue1150_ranked_coverage.py::test_first_json_returns_none_for_empty_text_and_broken_objects` | `src/adversarial.py::_first_json` | 200 | exit 1 / exit 0 | 7.413 |
| 06 | `tests/test_issue1150_ranked_coverage.py::test_recorded_verdict_is_unknown_when_the_brain_cannot_be_read` | `src/adversarial.py::recorded_verdict` | 211 | exit 1 / exit 0 | 7.472 |
| 07 | `tests/test_issue1150_ranked_coverage.py::test_recorded_verdict_skips_malformed_payloads_and_unreadable_artifacts` | `src/adversarial.py::recorded_verdict` | 271 | exit 1 / exit 0 | 7.383 |
| 08 | `tests/test_issue1150_ranked_coverage.py::test_panel_artifact_write_and_record_report_io_and_brain_failures` | `src/adversarial.py::_record_panel` | 302 | exit 1 / exit 0 | 7.374 |
| 09 | `tests/test_issue1150_ranked_coverage.py::test_worktree_head_and_git_boundary_fail_closed` | `src/adversarial.py::_worktree_head` | 310 | exit 1 / exit 0 | 7.339 |
| 10 | `tests/test_issue1150_ranked_coverage.py::test_review_at_head_rejects_a_bad_checkout_and_a_failed_provision` | `src/adversarial.py::review_at_head` | 328 | exit 1 / exit 0 | 7.348 |
| 11 | `tests/test_issue1150_ranked_coverage.py::test_review_at_head_fails_the_seat_and_a_restore_that_leaves_the_tree_dirty` | `src/adversarial.py::review_at_head` | 387 | exit 1 / exit 0 | 7.413 |
| 12 | `tests/test_issue1150_ranked_coverage.py::test_followup_lock_invalid_state_bad_labels_and_source_closure` | `src/adversarial.py::file_followup` | 401 | exit 1 / exit 0 | 7.458 |
| 13 | `tests/test_issue1150_ranked_coverage.py::test_spawn_post_merge_panel_fails_on_lookup_and_on_popen` | `src/adversarial.py::spawn_post_merge_panel` | 489 | exit 1 / exit 0 | 7.373 |
| 14 | `tests/test_issue1150_ranked_coverage.py::test_capability_heartbeat_swallows_recorder_failures` | `src/adversarial.py::_capability_heartbeat` | 523 | exit 1 / exit 0 | 7.562 |
| 15 | `tests/test_issue1150_ranked_coverage.py::test_review_cli_followup_and_usage` | `src/adversarial.py::_review_cli` | 533 | exit 1 / exit 0 | 8.502 |
| 16 | `tests/test_issue1150_ranked_coverage.py::test_unsafe_command_and_unknown_check_evidence` | `src/runtime_ac.py::_unsafe_command_reason` | 560 | exit 1 / exit 0 | 7.627 |
| 17 | `tests/test_issue1150_ranked_coverage.py::test_validate_spec_skips_non_objects_rejects_bad_checks_and_ratio` | `src/runtime_ac.py::validate_spec` | 577 | exit 1 / exit 0 | 7.832 |
| 18 | `tests/test_issue1150_ranked_coverage.py::test_dry_run_plan_copies_regex_pattern_and_warns_when_validation_is_bypassed` | `src/runtime_ac.py::build_dry_run_plan` | 593 | exit 1 / exit 0 | 7.621 |
| 19 | `tests/test_issue1150_ranked_coverage.py::test_bounded_break_result_and_regex_command_reject_bad_payloads` | `src/runtime_ac.py::_completed_result` | 614 | exit 1 / exit 0 | 7.508 |
| 20 | `tests/test_issue1150_ranked_coverage.py::test_run_verification_errors_when_a_command_check_has_no_command` | `src/runtime_ac.py::run_verification` | 638 | exit 1 / exit 0 | 7.525 |
| 21 | `tests/test_issue1150_ranked_coverage.py::test_author_issue_spec_rejects_a_target_that_cannot_become_a_slug` | `src/runtime_ac.py::author_issue_spec` | 642 | exit 1 / exit 0 | 7.649 |
| 22 | `tests/test_issue1150_ranked_coverage.py::test_runtime_ac_cli_validate_plan_run_results_and_goal` | `src/runtime_ac.py::main` | 649 | exit 1 / exit 0 | 7.585 |
| 23 | `tests/test_issue1150_ranked_coverage.py::test_pr_number_run_json_and_linked_issue_filters` | `src/keepalive_evidence.py::_pr_number` | 719 | exit 1 / exit 0 | 7.598 |
| 24 | `tests/test_issue1150_ranked_coverage.py::test_dedupe_skips_blank_open_issues_and_repeated_search_hits` | `src/keepalive_evidence.py::_dedupe_candidate` | 765 | exit 1 / exit 0 | 7.483 |
| 25 | `tests/test_issue1150_ranked_coverage.py::test_gotcha_section_ends_at_the_next_top_level_item_and_tooling_seed` | `src/keepalive_evidence.py::_process_signal_seed` | 774 | exit 1 / exit 0 | 7.456 |
| 26 | `tests/test_issue1150_ranked_coverage.py::test_human_summary_lists_candidates_and_the_empty_case` | `src/keepalive_evidence.py::_human_summary` | 795 | exit 1 / exit 0 | 7.666 |
| 27 | `tests/test_issue1150_ranked_coverage.py::test_retirement_helpers_and_cli_are_inert_without_a_declared_hold` | `src/cadence_registry.py::re_enable_when` | 814 | exit 1 / exit 0 | 7.373 |
| 28 | `tests/test_issue1150_ranked_coverage.py::test_unevaluated_ids_and_round_identity_fail_closed_without_tables` | `src/research_subjects.py::unevaluated_experiment_ids` | 831 | exit 1 / exit 0 | 7.492 |
| 29 | `tests/test_issue1150_ranked_coverage.py::test_round_arms_ignore_corrupt_json_and_findings_require_a_real_arm` | `src/research_subjects.py::round_arm_evidence` | 845 | exit 1 / exit 0 | 8.255 |
| 30 | `tests/test_issue1150_ranked_coverage.py::test_implementation_binding_skips_corrupt_events_and_names_an_unscored_run` | `src/research_subjects.py::resolve_round_durability` | 898 | exit 1 / exit 0 | 7.823 |
| 31 | `tests/test_issue1150_ranked_coverage.py::test_durability_edge_failure_is_counted_not_fatal` | `src/research_subjects.py::resolve_round_durability` | 930 | exit 1 / exit 0 | 7.486 |
| 32 | `tests/test_issue1150_ranked_coverage.py::test_subject_admission_cooldown_cap_and_lifecycle_edges` | `src/research_subjects.py::assess_candidate` | 956 | exit 1 / exit 0 | 7.483 |
| 33 | `tests/test_issue1150_ranked_coverage.py::test_weights_and_summary_close_an_empty_brain` | `src/research_subjects.py::effective_evidence_weights` | 989 | exit 1 / exit 0 | 7.392 |
| 34 | `tests/test_issue1150_ranked_coverage.py::test_weights_close_after_reading_a_real_brain` | `src/research_subjects.py::effective_evidence_weights` | 999 | exit 1 / exit 0 | 7.517 |
| 35 | `tests/test_issue1150_ranked_coverage.py::test_rounds_report_counts_failures_and_summary_survives_a_broken_report` | `src/research_subjects.py::rounds_report` | 1015 | exit 1 / exit 0 | 7.442 |
| 36 | `tests/test_issue1150_ranked_coverage.py::test_research_cli_records_findings_and_reports_refusals` | `src/research_subjects.py::main` | 1041 | exit 1 / exit 0 | 7.667 |

Focused module output:

```text
....................................                                     [100%]
tracked test inputs: armed -- watching 3 file(s) that `git archive HEAD tests` leaves out
36 passed in 0.78s
```

Final mutation runner output:

```text
Outcome 1 = 1/1; case 01: PASS (7.859s)
Outcome 1 = 2/2; case 02: PASS (7.4s)
Outcome 1 = 3/3; case 03: PASS (7.483s)
Outcome 1 = 4/4; case 04: PASS (7.435s)
Outcome 1 = 5/5; case 05: PASS (7.413s)
Outcome 1 = 6/6; case 06: PASS (7.472s)
Outcome 1 = 7/7; case 07: PASS (7.383s)
Outcome 1 = 8/8; case 08: PASS (7.374s)
Outcome 1 = 9/9; case 09: PASS (7.339s)
Outcome 1 = 10/10; case 10: PASS (7.348s)
Outcome 1 = 11/11; case 11: PASS (7.413s)
Outcome 1 = 12/12; case 12: PASS (7.458s)
Outcome 1 = 13/13; case 13: PASS (7.373s)
Outcome 1 = 14/14; case 14: PASS (7.562s)
Outcome 1 = 15/15; case 15: PASS (8.502s)
Outcome 1 = 16/16; case 16: PASS (7.627s)
Outcome 1 = 17/17; case 17: PASS (7.832s)
Outcome 1 = 18/18; case 18: PASS (7.621s)
Outcome 1 = 19/19; case 19: PASS (7.508s)
Outcome 1 = 20/20; case 20: PASS (7.525s)
Outcome 1 = 21/21; case 21: PASS (7.649s)
Outcome 1 = 22/22; case 22: PASS (7.585s)
Outcome 1 = 23/23; case 23: PASS (7.598s)
Outcome 1 = 24/24; case 24: PASS (7.483s)
Outcome 1 = 25/25; case 25: PASS (7.456s)
Outcome 1 = 26/26; case 26: PASS (7.666s)
Outcome 1 = 27/27; case 27: PASS (7.373s)
Outcome 1 = 28/28; case 28: PASS (7.492s)
Outcome 1 = 29/29; case 29: PASS (8.255s)
Outcome 1 = 30/30; case 30: PASS (7.823s)
Outcome 1 = 31/31; case 31: PASS (7.486s)
Outcome 1 = 32/32; case 32: PASS (7.483s)
Outcome 1 = 33/33; case 33: PASS (7.392s)
Outcome 1 = 34/34; case 34: PASS (7.517s)
Outcome 1 = 35/35; case 35: PASS (7.442s)
Outcome 1 = 36/36; case 36: PASS (7.667s)
```
