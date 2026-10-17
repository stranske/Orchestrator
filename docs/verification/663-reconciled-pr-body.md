Bind route-weight publication to `feedback.resolve_db_path`, refuse an empty or wholly unevidenced source, and let partial evidence regressions drain after a bounded hold. Capacity-only changes with identical `task_types` publish with the published evidence unchanged.

Closes #660

## Review fixes

Commit: `4f9f397cff613bdd3c1a8e3489c381bc4dcd9979` (merge of main plus the review fixes; includes the requested Codex co-author trailer).

- Merged `origin/main` (`c6c9a21`) and reconciled both independently added export guards and tests. Resolved `.verify-floor.json` using a full verification run rather than collection-only evidence.
- Added `EVIDENCE_REGRESSION_HOLD_HOURS = 24` and local `route-weights-regression-hold.json` with `first_seen`, regressing task types, and reason. A fresh partial regression refuses; the same regression publishes after the hold. Recovery clears the hold; a changed regressing task set restarts it.
- Version-zero/invalid versions and all-false task evidence remain absolute refusals, including with `--allow-evidence-regression`. This flag bypasses only a partial regression's hold. Every refusal prints `refused: regressing_tasks=<n>, drainable_in=<h>h`; absolute refusals report `infiniteh` because no age can authorize an empty source.
- Publish commit messages include the resolved database path, `source_version`, and `git config user.name` (fallback `orchestrator-export`). Tests assert both configured identity and fallback.
- Removed `docs/verification/issue-660-shared-db-resolver.md`. Correction to the previous description: `feedback.py` **does expose `resolve_db_path`**; the exporter calls that shared resolver. DB precedence is explicit CLI DB, `ORCH_FEEDBACK_DB`, `ORCH_LOCAL_RUNTIME`, state-directory fallback, then shared default.

## Latched-gate check

- **What decrements?** Elapsed wall-clock time reduces the remaining `EVIDENCE_REGRESSION_HOLD_HOURS` hold. Evidence recovery clears it. Changing the regressing task set starts a fresh hold.
- **Can it run while closed?** Yes. The cadence continues invoking the exporter after refusals, and the timestamp ages independently of successful publication.
- **One window?** Yes: one constant, `EVIDENCE_REGRESSION_HOLD_HOURS = 24`, sets both the measured hold and drain threshold.
- **What prints when drained?** `evidence regression held 24h; publishing with evidence_ok=false for review`. The real-local-remote test `tests/test_route_weights_export.py::test_publish_guard_preserves_remote_until_explicit_override[lost-review-refused-aged]` asserts this exact line, successful CLI exit, changed remote head, and the actual downgraded remote JSON. The fresh case asserts `regressing_tasks=1, drainable_in=24h`, saved hold metadata, unchanged bytes/head, and refusal before branch switching. Capacity-only cases assert unchanged task evidence; aged zero-version/all-false cases remain refused.

## Validation

Export tests: `/opt/anaconda3/bin/python3 -m pytest -q tests/test_route_weights_export.py` — **64 passed in 14.29s**. Changed Python passes Ruff and Black; staged diff has no whitespace errors.

Combined export/cadence/heartbeat suite after updating publisher test doubles for the provenance arguments: **89 passed in 19.48s**. Ruff and Black pass for all four changed Python files.


## Final full verification

`TMPDIR=/private/tmp /opt/anaconda3/bin/python3 src/verify.py` exited **1**. Exact verifier summary:

```text
# verify.py

  tree:       checkout
  state:      private copy, deleted after the run — ledger 22.4 MB; Brain 98.0 MB
  time:       pytest 11m24s, selftests 34s (8 at a time), gates 42s
  pytest:     3893 passed, 29 failed, 0/31 max skipped (3922 collected; floor 3922)
  selftests:  103 of 106 modules ran, 0/7 max skipped
  activation audit   ok  CAN FIRE:   48 of 48 live  (100%)
  recurrence replay  ok  WOULD FIRE:  48 of 53
  set coverage       ok  all 10 capability-set coverage checks passed over ALL 49 ledger capabilities (--roster for the per-capability table); 10 of 10 verdicts from this run's pytest
  admission          ok  all 24 admission checks passed — 12 enforced, 26 legacy debt, commitments clean (24 of 24 verdicts from this run's pytest)
  ledger validate    ok  "valid": true
  declared:   all 18 rows this tree declares are registered in the ledger judged
  entrypoints: NOT CHECKED (PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capabilities.json.lock')
  mypy ratchet: 0/0 max of 124 module(s) exempt, 124 checked — fully drained
```

The **29 final pytest failures exactly match the failures reproduced on unchanged main below**. The three failing module selftests are `capability_propensity`, `exp_abcd`, and `tick_watchdog`. The last two reproduce in the isolated main archive; the first also reproduces on unchanged main under this runtime-root topology, as detailed below. Full verification remains RED. `.verify-floor.json` now records **3922 collected / 3893 passed**, measured from this complete run; the passing floor is not above actual passes, and every skip/selftest/gate/mypy ceiling is unchanged. The first run and final run both executed the full suite; neither is collection-only evidence.

## Baseline comparison

An isolated archive of unchanged main `c6c9a21`, with private ledger/Brain state and `TMPDIR=/private/tmp`, reproduced **29 of the 33 selected pre-existing pytest failures** (4 passed), plus the same two module selftest failures: `exp_abcd` (denied write to the Vibe runtime config) and `tick_watchdog` (denied process-table access). A second unchanged-main archive placed temporarily under this worktree also reproduced `capability_propensity`: its audit treats imports of source and bytecode beneath the Orchestrator runtime root as live-state access. That probe directory was removed afterward. This establishes baseline reproduction for all three final selftest failures; it does not make full verification green. Those 29 pytest failures are listed below. They involve denied sandbox writes, `ps`/native sandbox execution, or inherited Gemini auth state. No privacy/security setting or test ceiling was changed.

```text
FAILED tests/test_agent_workspace_never_broad.py::test_every_evaluator_runs_in_a_scratch_workspace_of_its_own[vibe]
FAILED tests/test_build_exec_mirror.py::test_the_documented_copier_builds_through_the_builder
FAILED tests/test_build_exec_mirror.py::test_the_390_guard_composes_with_the_documented_copier
FAILED tests/test_build_exec_mirror.py::test_presync_says_whether_ci_verified_the_shipped_tree[documented]
FAILED tests/test_build_exec_mirror.py::test_presync_says_whether_ci_verified_the_shipped_tree[drifted]
FAILED tests/test_credential_redaction.py::test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain[works]
FAILED tests/test_credential_redaction.py::test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain[fails]
FAILED tests/test_exp_abcd_selftest_private_brain.py::test_every_brain_connection_in_the_selftest_opens_a_private_store
FAILED tests/test_experiment_arm_identity.py::test_arm_evaluate_dual_writes_exact_v2_and_parent_legacy
FAILED tests/test_model_profile_trial.py::test_three_profiles_create_distinct_worker_attempts
FAILED tests/test_model_profile_trial.py::test_evaluator_trace_stays_separate
FAILED tests/test_model_profile_trial.py::test_trial_readiness_surfaces_in_reports_and_lifecycle
FAILED tests/test_model_tier_resolution.py::test_capacity_sheds_seat_when_model_unresolvable
FAILED tests/test_model_tier_resolution.py::test_capacity_stays_ok_when_model_resolves
FAILED tests/test_model_tier_resolution.py::test_offload_survives_a_heartbeat_failure
FAILED tests/test_seatbelt_regex_quoting.py::SeatbeltRegexQuotingTests::test_backslash
FAILED tests/test_seatbelt_regex_quoting.py::SeatbeltRegexQuotingTests::test_combined
FAILED tests/test_seatbelt_regex_quoting.py::SeatbeltRegexQuotingTests::test_quote
FAILED tests/test_seatbelt_regex_quoting.py::SeatbeltRegexQuotingTests::test_regex_metacharacters
FAILED tests/test_tick_watchdog.py::test_a_stuck_tick_is_reported_with_the_kill_that_frees_it
FAILED tests/test_tick_watchdog.py::test_the_process_table_keeps_whole_command_lines
FAILED tests/test_tick_watchdog.py::test_the_drained_line_is_reachable_and_exact
FAILED tests/test_tick_watchdog.py::test_a_tick_that_reaches_no_cadence_step_alerts
FAILED tests/test_tick_watchdog.py::test_a_previous_tick_that_left_no_end_is_unknown_and_kept
FAILED tests/test_tick_watchdog.py::test_an_unreadable_record_is_unknown_not_clean
FAILED tests/test_tick_watchdog.py::test_the_arming_block_as_written_arms_and_records_the_tick
FAILED tests/test_tick_watchdog.py::test_fleet_summary_carries_the_tick_record
FAILED tests/test_tick_watchdog.py::test_a_drain_whose_target_has_exited_is_never_advertised
FAILED tests/test_tick_watchdog.py::test_ticks_that_never_reach_a_cadence_step_alert_from_first_arming
```

The first full run (before the publisher test-double corrections and safe temporary-root selection) exited 1: 3,886 passed, 36 failed, zero skipped, 3,922 collected; 311 subtests passed; four module selftests failed (`adapters`, `capability_propensity`, `exp_abcd`, `tick_watchdog`). Its three integration mock failures were fixed. Moving temporary state outside the live tree resolves the four private-state pytest failures and the `adapters` selftest failure and the earlier nested-temporary-state form of the `capability_propensity` failure; source-import auditing still fails in this worktree topology. It does not remove the 29 baseline failures above.

## Pending acceptance

Full verification is RED while the pre-existing failures remain. Hosted CI has not been waited on. Post-merge mirror sync, a real live-DB publish without the override, and remote readback (`source_version > 0`, `implement` ranking length >= 2) remain pending. Local bare-remote tests and the earlier read-only live DB preflight do not establish live publication acceptance.

## Historical deliberate-break evidence (original PR run)

The original empty-DB test ran against an isolated source copy whose `publish_guard` was temporarily changed to return `(True, "")` unconditionally. The copied source was restored byte-for-byte for that earlier run. These verbatim captures predate the review fixes above and do not claim final-head mutation proof.

Broken guard:

```text
Gate: pytest -q --tb=short tests/test_route_weights_export.py::test_publish_guard_refuses_empty_db[empty-db]
Source: isolated exporter copy; publish_guard returns (True, "") unconditionally
SHA256=940c6935783b6cbf537c8e255133b4dc556a28665fd48ae31fb344a846e69d61
F                                                                        [100%]
=================================== FAILURES ===================================
________________ test_publish_guard_refuses_empty_db[empty-db] _________________
tests/test_route_weights_export.py:479: in test_publish_guard_refuses_empty_db
    assert export.main() == 1, "publish_guard must refuse the empty DB"
           ^^^^^^^^^^^^^
/private/tmp/issue-660-mutation-final-6pyv73gj/route_weights_export.py:579: in main
    published = publish_document(document)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^
/private/tmp/issue-660-mutation-final-6pyv73gj/route_weights_export.py:375: in publish_document
    canonical = provision.ensure_canonical("stranske/Orchestrator")
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/test_route_weights_export.py:466: in <lambda>
    lambda *a: pytest.fail("publish_guard must refuse an empty DB before provisioning"),
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: publish_guard must refuse an empty DB before provisioning
----------------------------- Captured stdout call -----------------------------
wrote /Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-2244/test_publish_guard_refuses_emp0/route-weights-export.json
----------------------------- Captured stderr call -----------------------------
db=/Users/teacher/.codex/orchestrator/agent-runtime/codex/tmp/pytest-of-teacher/pytest-2244/test_publish_guard_refuses_emp0/empty.db source_version=0
=========================== short test summary info ============================
FAILED tests/test_route_weights_export.py::test_publish_guard_refuses_empty_db[empty-db]
1 failed in 0.46s
EXIT_CODE=1
```

Restored guard:

```text
Gate: pytest -q --tb=short tests/test_route_weights_export.py::test_publish_guard_refuses_empty_db[empty-db]
Source: isolated exporter copy; original source restored byte-for-byte
SHA256=e5ff307fed5bdb29b4fd3852020ec392065d781139a37597d4969153f1f78570
.                                                                        [100%]
1 passed in 0.44s
EXIT_CODE=0
```

<!-- pr-preamble:start -->
<!-- meta:issue:660 -->
> **Source:** Issue #660

Closes #660

<!-- pr-preamble:end -->

<!-- auto-status-summary:start -->
## Automated Status Summary
#### Scope
The route-weights export published to `exports/route-weights/config/route-weights.json` at 2026-10-17T03:53:44Z carries `source_version: 0` and `evidence_ok: false` with `ranking: []` for every task type, while the live Brain at `~/.codex/orchestrator/feedback/orchestrator.db` is at weight version 68 with `implement` observations codex 1198, cursor 445, gemini 101, vibe 137, claude 450 (read-only check, 2026-10-17 04:30Z). Rebuilding the export from the live DB yields `implement` ranking length 4. The publisher therefore ran against an empty or wrong database and replaced an evidenced publication with an evidence-less one; the change detector accepted it because the content differed. Every consumer of the export (stranske/Workflows `agent_delegation_policy.js`, the intake chooser from Workflows#3991) now falls back to its static default, so the learned routing that Priority 3 depends on is silently off. Nothing reported it: `route_weights_export.py` neither binds the DB path explicitly nor refuses to publish a regression from evidenced to evidence-less content.

Campaign concept (value-chain gate): situation = every daily export publish; demand = one publish per day plus every capacity-state change; pass number = the published export has `source_version > 0` and `task_types.implement.ranking` length ≥ 2 on every check, and the exporter's log shows one refused publish when fed an empty DB in the test; printed by `gh api repos/stranske/Orchestrator/contents/config/route-weights.json?ref=exports/route-weights` and the exporter's test. Check date: next daily export after merge, then the weekly check 2026-10-20.

<!-- Updated WORKFLOW_OUTPUTS.md context:start -->
## Context for Agent

### Related Issues/PRs
- [stranske/Orchestrator#575](https://github.com/stranske/Orchestrator/issues/575)
<!-- Updated WORKFLOW_OUTPUTS.md context:end -->

#### Tasks
- [x] Update `src/route_weights_export.py` to use the shared `feedback.py` DB resolver and log `db=<path> source_version=<n>` at the start of every run.
- [x] Add `publish_guard(new_doc, published_doc) -> (ok, reason)` in `src/route_weights_export.py`; refuse publication and exit non-zero with the reason for the three regression conditions.
- [x] Add `--allow-evidence-regression` to `src/route_weights_export.py` and log deliberate allowed regressions.
- [x] Add tests in `tests/test_route_weights_export.py` for empty DB refusal, all-false evidence refusal, published-true/new-false refusal, flag allowance, and `ORCH_STATE_DIR` path resolution.
- [x] Update the export README and `ARCHITECTURE.md` export paragraph with the guard.
- [x] Update `orchestrate.sh` cadence logging to surface export-publish refusals.
- [ ] Run one publish from the live DB after merge and mirror sync; record `source_version` and `implement` ranking length in the PR.

#### Acceptance criteria
- [x] `pytest tests/test_route_weights_export.py` passes.
- [x] Making `publish_guard` return `(True, "")` unconditionally causes the empty-DB test to fail naming the guard; reverting it makes the test pass, with both outcomes captured in the PR body.
- [ ] `python3 src/verify.py` passes.
- [ ] The next published export has `source_version > 0` and `task_types.implement.ranking` length ≥ 2.

<!-- auto-status-summary:end -->

## Keepalive task reconciliation (2026-10-17)

Reviewed commits `4f9f397` through `c3b5f81`. The export-test criterion is checked based on the recorded 64-test passing run above; a final-head rerun is still needed. Full verification remains unchecked because the recorded full run failed. Reopened the live-publish task and readback criterion: this PR is open and the pending-acceptance notes explicitly say post-merge sync/publication has not occurred. A prior read-only DB check or local bare-remote fixture is not post-merge live publication evidence.

## Current keepalive validation and blockers

- Added `tests/test_route_weights_export_cli.js`: eight subprocess cases cover missing DB, zero-byte DB, empty weights table, and nonzero-version weights below the observation threshold, each with and without `--allow-evidence-regression`. Each asserts exit 1, database provenance, the specific guard refusal, no Git invocation, unchanged source bytes (or no missing-DB creation), and the retained local shadow export. The subprocess uses a private home, runtime, handoff, and capability ledger; no provider CLI is on its PATH.
- `node --test --test-isolation=none tests/test_route_weights_export_cli.js`: **8 passed, 0 failed**, approximately 3.8 seconds. Standard `node --test tests/test_route_weights_export_cli.js` also exits 0 (this runner summarizes its child file as one passing test).
- Deliberately replacing `publish_guard` with `return True, ""` in an isolated `/tmp` source copy makes **all 8 cases fail** on the expected `/publish_guard refused:/` assertion. The tracked source was never modified. The real source passes the same cases. This fresh mutation check supplements the historical pytest mutation evidence above; it does not claim a new pytest run.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_route_weights_publication.py -v`: **2 tests passed** (including the existing real-local-remote publication cases and URI-path subtests).
- `node --check tests/test_route_weights_export_cli.js` and whitespace checks pass. No Python files changed. Black is unavailable (`No module named black`); no Python commit or formatting pass is claimed.
- Fresh targeted pytest cannot run: both `pytest` and the Python pytest module are absent. Installing pytest and Black from PyPI into `/tmp` failed with no available distributions. The verifier's pytest runner confirms `collected: 0`, exit 1, and `No module named pytest`, so full acceptance cannot be green here. The full `python3 src/verify.py` invocation is still running its remaining phases at the time of this note.
- Read back the published JSON through the GitHub connector: blob `0faee6881dfbb9cd625be1428455b151f801b269`, generated `2026-10-17T04:02:41Z`, `source_version: 68`, `implement.evidence_ok: true`, **4 implement rankings**. This establishes that the currently served artifact is evidenced. It does not establish a post-merge publish/sync of this still-open PR or the next scheduled publication check.
- PR #663 was verified **open, draft=false**, head `c3b5f81040ca4fb9daf48ea1f57c97d722383698`.
- The connector rejected the PR-body update: `MCP tool call requires approval, but approval policy is never`. This file is the prepared replacement body, not a claim that GitHub was updated.
- Committing the JavaScript test was attempted but blocked: Git could not create `.git/index.lock` because `.git` is read-only. Source and reconciliation changes remain uncommitted for the authorized automation runner to apply. No protected workflow files were changed.
