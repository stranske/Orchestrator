# Issue 679 review-fix verification

Runtime: `/opt/anaconda3/bin/python3`, with `TMPDIR=/private/tmp` and `ORCH_MODEL_PROBE=0`.
The supplied PR worktree already includes main `8c53ae1ae3cec9c67a2fa12b3ad56cec14d1dec5`.
GitHub inspection and PR-body updates use REST `gh api` only.

## Deliberate breaks

- `sticky-red.log`: replace the persisted `extended` check with `False` in
  `src/route_weights_export.py`; run
  `python3 -m pytest -q tests/test_exploration_window_extension.py::test_extended_state_is_sticky`.
  Exit 1; the second export incorrectly resets the extension after enough samples arrive.
- `count-identity-red.log`: remove `policy_route_weights_exploration` from the aggregate counts in
  `src/agent_switches.py`; run
  `python3 -m pytest -q tests/test_agent_switches_delegation_source.py::test_policy_source_counts_sum_equals_policy_switches`.
  Exit 1; the four source counts sum to 3 while total policy switches remain 4.
- Each source mutation was restored in a `finally` block before rerunning its test.
  The corresponding `*-green.log` captures each restored test passing (exit 0).

## Policy attribution

`exploration_collection.choose_mode({}, now=switched_ts)` is deterministic: with no deficits,
UTC week parity selects the policy. `_exploration_block` uses precisely this call for the hosted
export. Counts query `agent_switches` through a SQLite `mode=ro` connection and retain unknowns
when the DB cannot be read. Missing databases are never created by this measurement.

## Validation

- `targeted.log`: the complete requested targeted pytest set.
- `collection.log`: independent full-tree collection; the floor records 4,074 collected and an
  acceptance expectation of 4,074 passed. No skip or other ceiling changed.
- Ruff and Black checks over the changed Python modules/tests and `/bin/bash -n orchestrate.sh` pass.

## Not done

`python3 src/verify.py` returned 1: 4,038 passed, 35 failed, 1 skipped (4,074 collected),
102 module selftests passed and four failed (`adapters`, `capability_propensity`, `exp_abcd`,
`tick_watchdog`). All five gates passed. Entrypoint checking was not completed:
`PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capabilities.json.lock'`.
No clean-main control was run; none of these failures is claimed to be pre-existing.

The full run used `ORCH_MODEL_PROBE=0`. Six mocked auth assertions pass when that flag is absent
(`auth-control.log`, exit 0); this control does not turn the full verifier green.
The full run began before the final shell trailing-newline hardening. The entire requested
143-test targeted set was rerun after that change and passed. The collection count is unchanged.

`verify.log` retains the verifier report. `pytest-failures.log` retains complete pytest tracebacks.
The four `selftest-*.log` files capture isolated reruns of the failing modules against a private
ledger/Brain snapshot; those rerun dispositions are recorded in `verification.json`.

Exact pytest errors from that full run follow (including environment-dependent results):

### tests.test_agent_workspace_never_broad::test_every_evaluator_runs_in_a_scratch_workspace_of_its_own[vibe]

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

### tests.test_build_exec_mirror::test_the_documented_copier_builds_through_the_builder

```text
AssertionError: mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.2CbUumSZs0: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 33 (rc 1): /private/tmp/pytest-of-teacher/pytest-160/test_the_documented_copier_bui0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  
assert 1 == 0
 +  where 1 = CompletedProcess(args=['bash', '/private/tmp/pytest-of-teacher/pytest-160/test_the_documented_copier_bui0/orch-sync-mi...the_documented_copier_bui0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.\n').returncode
```

### tests.test_build_exec_mirror::test_the_390_guard_composes_with_the_documented_copier

```text
AssertionError: mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.QZUiirCypX: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 38 (rc 1): /private/tmp/pytest-of-teacher/pytest-160/test_the_390_guard_composes_wi0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  
assert 1 == 0
 +  where 1 = CompletedProcess(args=['bash', '/private/tmp/pytest-of-teacher/pytest-160/test_the_390_guard_composes_wi0/orch-sync-mi...the_390_guard_composes_wi0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.\n').returncode
```

### tests.test_build_exec_mirror::test_presync_says_whether_ci_verified_the_shipped_tree[documented]

```text
AssertionError: == scratch mirror of /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v1/source @ c59aa1c (0 uncommitted) -> /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v1/verify-before-sync.VE0fYX/mirror
  tests/ shipped from git HEAD c59aa1c (2 files) — uncommitted test edits are NOT shipped
  scripts/ shipped from git HEAD (4 entries)
  .github/ shipped from git HEAD (1 files) with .gitignore ruff.toml
  docs/ shipped from git HEAD (1 files, merged beside run-time output)
  built 3 .py (from /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v1/source/src) + orchestrate.sh + 1 test file(s) + 11 data/config entries -> /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v1/verify-before-sync.VE0fYX/mirror
  mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.vYKdGdOqmJ: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 33 (rc 1): /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v1/verify-before-sync.VE0fYX/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  verify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched
  
assert 2 == 0
 +  where 2 = CompletedProcess(args=['bash', '/Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/scrip...verify.\nverify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched\n').returncode
```

### tests.test_build_exec_mirror::test_presync_says_whether_ci_verified_the_shipped_tree[drifted]

```text
AssertionError: == scratch mirror of /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v2/source @ 241a0b9 (0 uncommitted) -> /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v2/verify-before-sync.ssol5A/mirror
  tests/ shipped from git HEAD 241a0b9 (2 files) — uncommitted test edits are NOT shipped
  scripts/ shipped from git HEAD (4 entries)
  .github/ shipped from git HEAD (1 files) with .gitignore ruff.toml
  docs/ shipped from git HEAD (1 files, merged beside run-time output)
  built 3 .py (from /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v2/source/src) + orchestrate.sh + 1 test file(s) + 11 data/config entries -> /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v2/verify-before-sync.ssol5A/mirror
  mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.8h1Ez13hHn: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 33 (rc 1): /private/tmp/pytest-of-teacher/pytest-160/test_presync_says_whether_ci_v2/verify-before-sync.ssol5A/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  verify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched
  
assert 2 == 0
 +  where 2 = CompletedProcess(args=['bash', '/Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/scrip...verify.\nverify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched\n').returncode
```

### tests.test_credential_redaction::test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain[works]

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

### tests.test_credential_redaction::test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain[fails]

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

### tests.test_exp_abcd_selftest_private_brain::test_every_brain_connection_in_the_selftest_opens_a_private_store

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

### tests.test_experiment_arm_identity::test_arm_evaluate_dual_writes_exact_v2_and_parent_legacy

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

### tests.test_model_profile_trial::test_three_profiles_create_distinct_worker_attempts

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capability-program/profile-trial.json.xy7u79ro.tmp'
```

### tests.test_model_profile_trial::test_evaluator_trace_stays_separate

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capability-program/profile-trial.json.hk281t_i.tmp'
```

### tests.test_model_profile_trial::test_trial_readiness_surfaces_in_reports_and_lifecycle

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capability-program/profile-trial.json.ib4q6rd5.tmp'
```

### tests.test_model_tier_resolution::test_auth_failure_is_detected

```text
AssertionError: {'agent': 'cursor', 'authenticated': True, 'checked': False, 'strength': None, ...}
assert (False)
```

### tests.test_model_tier_resolution::test_auth_ok_when_probe_succeeds

```text
AssertionError: {'agent': 'cursor', 'authenticated': True, 'checked': False, 'strength': None, ...}
assert (True and False)
```

### tests.test_model_tier_resolution::test_exit_zero_but_failed_is_still_a_failure

```text
AssertionError: {'agent': 'cursor', 'authenticated': True, 'checked': False, 'strength': None, ...}
assert (False)
```

### tests.test_model_tier_resolution::test_status_style_not_logged_in_is_caught

```text
assert not True
```

### tests.test_model_tier_resolution::test_presence_probe_still_detects_a_missing_credential

```text
AssertionError: {'agent': 'claude', 'authenticated': True, 'checked': False, 'strength': None, ...}
assert (False)
```

### tests.test_model_tier_resolution::test_every_seat_has_some_free_signal

```text
AssertionError: ('codex', {'agent': 'codex', 'verdict': 'UNKNOWN', 'detail': 'no auth probe for this agent', 'tier_models': {'cheap': 'gpt-6-luna', 'mid': 'gpt-5.6-terra', 'full': 'gpt-6.1-sol'}, ...})
assert 'UNKNOWN' != 'UNKNOWN'
```

### tests.test_model_tier_resolution::test_presence_pass_is_not_reported_as_verified

```text
AssertionError: assert 'CONFIGURED' == 'PRESENT'
  
  - PRESENT
  + CONFIGURED
```

### tests.test_model_tier_resolution::test_offload_survives_a_heartbeat_failure

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/handoff/dispatch-logs/offload.cursor.1792259838168016000.log'
```

### tests.test_muse_backend::test_muse_seat_is_reported_and_never_reads_as_free

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capacity-resets.tmp'
```

### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_backslash

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_combined

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_quote

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_regex_metacharacters

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

### tests.test_tick_watchdog::test_a_stuck_tick_is_reported_with_the_kill_that_frees_it

```text
AssertionError: timed out after 60s waiting for a drain naming the stuck command; record={}; history=<none>
```

### tests.test_tick_watchdog::test_the_process_table_keeps_whole_command_lines

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

### tests.test_tick_watchdog::test_the_drained_line_is_reachable_and_exact

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

### tests.test_tick_watchdog::test_a_tick_that_reaches_no_cadence_step_alerts

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

### tests.test_tick_watchdog::test_a_previous_tick_that_left_no_end_is_unknown_and_kept

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

### tests.test_tick_watchdog::test_an_unreadable_record_is_unknown_not_clean

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

### tests.test_tick_watchdog::test_the_arming_block_as_written_arms_and_records_the_tick

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
    WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)
  
assert False
 +  where False = <built-in method startswith of str object at 0x125963960>('  tick-watchdog: armed')
 +    where <built-in method startswith of str object at 0x125963960> = "  WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed\n  WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)\n".startswith
 +      where "  WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed\n  WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)\n" = CompletedProcess(args=['bash', '-c', 'set -euo pipefail\nORCH=/Users/teacher/.codex/orchestrator/worktrees/stranske__O...s'); not armed\n  WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)\n", stderr='').stdout
```

### tests.test_tick_watchdog::test_fleet_summary_carries_the_tick_record

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

### tests.test_tick_watchdog::test_a_drain_whose_target_has_exited_is_never_advertised

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

### tests.test_tick_watchdog::test_ticks_that_never_reach_a_cadence_step_alert_from_first_arming

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

## Isolated selftest errors

### adapters

```text
AssertionError: []
```

### capability_propensity

```text
AssertionError: a propensity selftest touched 21 live location(s), first ['open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/research_subjects.cpython-312.pyc', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/capability_advisor.cpython-312.pyc', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/capability_advisor.py', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/env_prereq.cpython-312.pyc', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/fleet_shapes.cpython-312.pyc']; give that path the private store instead (see _private_live_state)
```

### exp_abcd

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

### tick_watchdog

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

