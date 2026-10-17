# Issue 679 review-fix evidence

Main `0c0059e19c868898df6506fc56c314a5971e5fa3` is merged. The floor conflict was resolved from
main's file, with `collected` and the `passed` expectation set to the independently observed
4,079 tests. Every ceiling is unchanged. Runtime: `/opt/anaconda3/bin/python3`.

## Review fixes

The owner decision q-1ca1d09d255e987b is implemented: the export measures policy exploration
switches since 2026-10-17 through a read-only SQLite connection, writes the window decision,
and extends from 20261117 to 20261215 only at/after the base end when a policy has fewer than
30 switches. Extension is sticky. With an empty plan, `choose_mode({}, now=switched_ts)` is
deterministic in UTC-week parity, exactly as in the hosted export's `_exploration_block`.

The Bash 3.2 prologue reads the JSON end, requires an eight-digit string, and falls back to
20261117 for missing, unreadable or malformed data. Length is checked before command
substitution can strip trailing newlines. The weekly review reads both dates from state and
reports unmeasured inputs. Exploration switches appear in source totals, transitions and text.

## Passing verification

- `targeted-merged.log`: all 143 requested targeted tests pass on the merged code, after the
  final type adjustment; `ORCH_MODEL_PROBE` is unset and `TMPDIR=/private/tmp`.
- `collection-merged.log`: 4,079 independently collected tests.
- `static-checks.log`: mypy over three changed modules, Black and Ruff over all changed Python
  files, and `/bin/bash -n orchestrate.sh` pass.
- `verification.json`: code hashes, commands, exact bases and separate verification dispositions.

## Deliberate breaks

Both production mutations were restored before the passing captures were taken:

- `sticky-red.log`: replace the persisted extension check with `False` and run
  `python3 -m pytest -q tests/test_exploration_window_extension.py::test_extended_state_is_sticky`.
  Exit 1: `assert False is True`. Restored: exit 0 in `sticky-green.log`.
- `count-identity-red.log`: remove the aggregate's `policy_route_weights_exploration` key and run
  `python3 -m pytest -q tests/test_agent_switches_delegation_source.py::test_policy_source_counts_sum_equals_policy_switches`.
  Exit 1: the source sum is 3, total switches 4. Restored: exit 0 in `count-identity-green.log`.

## Not done

The full verifier returned 1: **4,049 passed, 30 failed, zero skipped** (4,079 collected),
103 module selftests passed and three failed. All five gates passed. `verify-merged.log` is the
actual report; `pytest-merged-failures.log` retains all exact pytest tracebacks.

All 30 pytest failures also failed in clean checkouts of main
`0c0059e19c868898df6506fc56c314a5971e5fa3`. Both controls used normal probes and private
ledger/Brain copies, and remained clean before cleanup. `main-control.log` / `main-control-results.json`
cover 28 failures plus seven passing auth cases; `main-auth-control.log` /
`main-auth-control-results.json` cover the two additional auth-capacity failures. Both temporary
checkouts and their scratch directories were removed after retaining the results.

No clean-main control was run for the three failing module selftests; their baseline is UNKNOWN.
Their isolated normal-probe rerun logs are `selftest-merged-*.log`. Entrypoint validation was also
not completed: `PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capabilities.json.lock'`.

The full run started at code commit `f360cf6`. During that run, an integer-count dictionary's
unmeasured representation was moved into JSON construction to satisfy mypy; public behavior is
unchanged. The complete 143-test targeted set and static checks passed after that refactor.
The full verifier is RED and is not presented as final-head green acceptance. Hosted CI is not awaited.

### Exact pytest errors

Every case below also failed on the clean-main control. These are exact captured error messages;
complete traces are in `pytest-merged-failures.log`.

#### tests.test_agent_workspace_never_broad::test_every_evaluator_runs_in_a_scratch_workspace_of_its_own[vibe]

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

#### tests.test_build_exec_mirror::test_the_documented_copier_builds_through_the_builder

```text
AssertionError: mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.svFFr518j8: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 33 (rc 1): /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_the_documented_copier_bui0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  
assert 1 == 0
 +  where 1 = CompletedProcess(args=['bash', '/private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_the_documented_copier_...the_documented_copier_bui0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.\n').returncode
```

#### tests.test_build_exec_mirror::test_the_390_guard_composes_with_the_documented_copier

```text
AssertionError: mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.M5DzUjmTEt: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 38 (rc 1): /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_the_390_guard_composes_wi0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  
assert 1 == 0
 +  where 1 = CompletedProcess(args=['bash', '/private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_the_390_guard_composes...the_390_guard_composes_wi0/stage/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.\n').returncode
```

#### tests.test_build_exec_mirror::test_presync_says_whether_ci_verified_the_shipped_tree[documented]

```text
AssertionError: == scratch mirror of /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v1/source @ 1490f7f (0 uncommitted) -> /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v1/verify-before-sync.5o0JG6/mirror
  tests/ shipped from git HEAD 1490f7f (2 files) — uncommitted test edits are NOT shipped
  scripts/ shipped from git HEAD (4 entries)
  .github/ shipped from git HEAD (1 files) with .gitignore ruff.toml
  docs/ shipped from git HEAD (1 files, merged beside run-time output)
  built 3 .py (from /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v1/source/src) + orchestrate.sh + 1 test file(s) + 11 data/config entries -> /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v1/verify-before-sync.5o0JG6/mirror
  mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.1I9ZMIucHS: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 33 (rc 1): /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v1/verify-before-sync.5o0JG6/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  verify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched
  
assert 2 == 0
 +  where 2 = CompletedProcess(args=['bash', '/Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/scrip...verify.\nverify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched\n').returncode
```

#### tests.test_build_exec_mirror::test_presync_says_whether_ci_verified_the_shipped_tree[drifted]

```text
AssertionError: == scratch mirror of /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v2/source @ 672bfcd (0 uncommitted) -> /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v2/verify-before-sync.Wq3iaO/mirror
  tests/ shipped from git HEAD 672bfcd (2 files) — uncommitted test edits are NOT shipped
  scripts/ shipped from git HEAD (4 entries)
  .github/ shipped from git HEAD (1 files) with .gitignore ruff.toml
  docs/ shipped from git HEAD (1 files, merged beside run-time output)
  built 3 .py (from /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v2/source/src) + orchestrate.sh + 1 test file(s) + 11 data/config entries -> /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v2/verify-before-sync.Wq3iaO/mirror
  mktemp: mkstemp failed on /var/folders/qm/w0dtc0j132gd6ymf1hxtd2p00000gp/T/tmp.qvD6Whmhiy: Operation not permitted
  ERR: orch-sync-mirror.sh aborted at line 33 (rc 1): /private/tmp/pr696-final-verify/pytest-of-teacher/pytest-0/test_presync_says_whether_ci_v2/verify-before-sync.Wq3iaO/mirror is HALF-SYNCED -- fix the cause and re-run before trusting a mirror verify.
  verify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched
  
assert 2 == 0
 +  where 2 = CompletedProcess(args=['bash', '/Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/scrip...verify.\nverify-before-sync: NOTHING VERIFIED: the scratch copy failed (see above); the live mirror was not touched\n').returncode
```

#### tests.test_credential_redaction::test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain[works]

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

#### tests.test_credential_redaction::test_an_experiment_evaluators_output_is_masked_before_it_reaches_the_brain[fails]

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

#### tests.test_exp_abcd_selftest_private_brain::test_every_brain_connection_in_the_selftest_opens_a_private_store

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

#### tests.test_experiment_arm_identity::test_arm_evaluate_dual_writes_exact_v2_and_parent_legacy

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

#### tests.test_model_profile_trial::test_three_profiles_create_distinct_worker_attempts

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capability-program/profile-trial.json.oadqnw38.tmp'
```

#### tests.test_model_profile_trial::test_evaluator_trace_stays_separate

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capability-program/profile-trial.json.0nzce0q0.tmp'
```

#### tests.test_model_profile_trial::test_trial_readiness_surfaces_in_reports_and_lifecycle

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capability-program/profile-trial.json.ar7v2_38.tmp'
```

#### tests.test_model_tier_resolution::test_capacity_sheds_seat_when_model_unresolvable

```text
AssertionError: {'policy': 'auth-failed', 'availability': 'unavailable_auth_failed', 'next_action': 'Refresh gemini credentials, then re-check with `python3 capacity.py --json`. The fleet reads the credential FILE, not an interactive login session.'}
assert 'unavailable_auth_failed' == 'unavailable_model_unresolved'
  
  - unavailable_model_unresolved
  + unavailable_auth_failed
```

#### tests.test_model_tier_resolution::test_capacity_stays_ok_when_model_resolves

```text
AssertionError: shed
assert 'shed' != 'shed'
 +  where 'shed' = capacity.SHED
```

#### tests.test_model_tier_resolution::test_offload_survives_a_heartbeat_failure

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/handoff/dispatch-logs/offload.cursor.1792261129823684000.log'
```

#### tests.test_muse_backend::test_muse_seat_is_reported_and_never_reads_as_free

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/capacity-resets.tmp'
```

#### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_backslash

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

#### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_combined

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

#### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_quote

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

#### tests.test_seatbelt_regex_quoting.SeatbeltRegexQuotingTests::test_regex_metacharacters

```text
AssertionError: stdout: 
  stderr: sandbox-exec: sandbox_apply: Operation not permitted
  
assert 71 == 0
 +  where 71 = CompletedProcess(args=['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny file-write* (subpath "/private/.../.git/hooks/pre-commit"]]'], returncode=71, stdout='', stderr='sandbox-exec: sandbox_apply: Operation not permitted\n').returncode
```

#### tests.test_tick_watchdog::test_a_stuck_tick_is_reported_with_the_kill_that_frees_it

```text
AssertionError: timed out after 60s waiting for a drain naming the stuck command; record={}; history=<none>
```

#### tests.test_tick_watchdog::test_the_process_table_keeps_whole_command_lines

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

#### tests.test_tick_watchdog::test_the_drained_line_is_reachable_and_exact

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

#### tests.test_tick_watchdog::test_a_tick_that_reaches_no_cadence_step_alerts

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

#### tests.test_tick_watchdog::test_a_previous_tick_that_left_no_end_is_unknown_and_kept

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

#### tests.test_tick_watchdog::test_an_unreadable_record_is_unknown_not_clean

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

#### tests.test_tick_watchdog::test_the_arming_block_as_written_arms_and_records_the_tick

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
    WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)
  
assert False
 +  where False = <built-in method startswith of str object at 0x1253e8120>('  tick-watchdog: armed')
 +    where <built-in method startswith of str object at 0x1253e8120> = "  WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed\n  WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)\n".startswith
 +      where "  WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed\n  WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)\n" = CompletedProcess(args=['bash', '-c', 'set -euo pipefail\nORCH=/Users/teacher/.codex/orchestrator/worktrees/stranske__O...s'); not armed\n  WARN: tick-watchdog did not arm -- nothing watches this tick (the line above says why)\n", stderr='').stdout
```

#### tests.test_tick_watchdog::test_fleet_summary_carries_the_tick_record

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

#### tests.test_tick_watchdog::test_a_drain_whose_target_has_exited_is_never_advertised

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

#### tests.test_tick_watchdog::test_ticks_that_never_reach_a_cadence_step_alert_from_first_arming

```text
AssertionError:   WARN: tick-watchdog: cannot read the process table ([Errno 1] Operation not permitted: 'ps'); not armed
  
assert 2 == 0
```

### Exact isolated selftest errors

#### capability_propensity (baseline UNKNOWN)

```text
AssertionError: a propensity selftest touched 21 live location(s), first ['open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/research_subjects.cpython-312.pyc', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/capability_advisor.cpython-312.pyc', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/capability_advisor.py', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/env_prereq.cpython-312.pyc', 'open /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__679__opener/src/__pycache__/fleet_shapes.cpython-312.pyc']; give that path the private store instead (see _private_live_state)
```

#### exp_abcd (baseline UNKNOWN)

```text
PermissionError: [Errno 1] Operation not permitted: '/Users/teacher/.codex/orchestrator/agent-runtime/vibe/.vibe/config.toml'
```

#### tick_watchdog (baseline UNKNOWN)

```text
PermissionError: [Errno 1] Operation not permitted: 'ps'
```

## Earlier captures

`verify.log`, `pytest-failures.log`, `targeted.log`, `collection.log`, `auth-control.log` and
`selftest-*.log` without `merged` record the earlier tree before main advanced. That full run
used `ORCH_MODEL_PROBE=0` and reported 35 pytest failures. The final normal-probe run and its
clean-main controls above supersede those earlier dispositions; original captures are retained.
