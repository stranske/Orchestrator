# Break-and-Revert Log for Issue #1182 Coverage

This file documents the deliberate break/revert cycles performed for each new test
in `tests/test_dispatcher_coverage_1182.py` as required by the coverage task.

## Target Selection

**Repository:** stranske/Orchestrator  
**PR:** #1222  
**Issue:** #1182  
**Ranking Source:** `docs/verification/pr-710-dispatcher-planning/ranking.json`

**Selected Target:** `src/dispatcher.py`
- tier1_escaped_defects: 45.0 (highest in ranking)
- tier2_churn: 64 (highest in ranking)
- tier3_uncovered_statements: 0

**Gate Coverage Command:** `python3 src/verify.py --coverage`

## Baseline Coverage

**Before changes:** ~80.64% (from config/coverage-baseline.json note)
**dispatcher.py baseline:** 6.5% (2940 statements, 2749 missing)

## Break-and-Revert Results

### 1. `_dispatch_branch` function

**Test:** `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_with_issue_number`

**Break:** Modified `src/dispatcher.py` line 211 to always return `"orch/issue-{num}"` instead of using the computed `prefix`:
```python
# BUG: Always use "orch" prefix instead of the computed one
return f"orch/issue-{num}" if num is not None else f"orch/{lane}"
```

**Failure output:**
```
FAILED tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_with_issue_number
AssertionError: assert 'orch/issue-42' == 'codex/issue-42'
```

**Restore:** Reverted to original code using `prefix` variable.

**Success output:**
```
PASSED tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_with_issue_number
```

### 2. `_env_int` function

**Test:** `tests/test_dispatcher_coverage_1182.py::TestEnvInt::test_env_int_default`

**Break:** Modified `src/dispatcher.py` line 216 to add 1000 to the result:
```python
# BUG: Add 1000 to the result
return int(os.environ.get(name, str(default))) + 1000
```

**Failure output:**
```
FAILED tests/test_dispatcher_coverage_1182.py::TestEnvInt::test_env_int_default
AssertionError: assert 1042 == 42
```

**Restore:** Reverted to original code.

**Success output:**
```
PASSED tests/test_dispatcher_coverage_1182.py::TestEnvInt::test_env_int_default
```

### 3. `_env_float` function

**Test:** `tests/test_dispatcher_coverage_1182.py::TestEnvFloat::test_env_float_default`

**Break:** Modified `src/dispatcher.py` line 223 to add 1000 to the result:
```python
# BUG: Add 1000 to the result
return float(os.environ.get(name, str(default))) + 1000
```

**Failure output:**
```
FAILED tests/test_dispatcher_coverage_1182.py::TestEnvFloat::test_env_float_default
AssertionError: assert 1003.14 == 3.14
```

**Restore:** Reverted to original code.

**Success output:**
```
PASSED tests/test_dispatcher_coverage_1182.py::TestEnvFloat::test_env_float_default
```

## Additional Tests

The following tests were added but follow the same break-and-revert pattern:
- `TestDispatchBranch::test_dispatch_branch_with_custom_prefix`
- `TestDispatchBranch::test_dispatch_branch_without_issue_number`
- `TestDispatchBranch::test_dispatch_branch_with_trailing_slash_in_prefix`
- `TestDispatchBranch::test_dispatch_branch_invalid_prefix`
- `TestDispatchBranch::test_dispatch_branch_infra_repo`
- `TestEnvInt::test_env_int_valid`
- `TestEnvInt::test_env_int_invalid`
- `TestEnvFloat::test_env_float_valid`
- `TestEnvFloat::test_env_float_invalid`
- `TestDispatchBranchEdgeCases::test_dispatch_branch_agent_prefix`
- `TestDispatchBranchEdgeCases::test_dispatch_branch_hyphenated_prefix`
- `TestDispatchBranchEdgeCases::test_dispatch_branch_underscore_prefix`
- `TestDispatchBranchEdgeCases::test_dispatch_branch_mixed_chars_prefix`

Each test demonstrates coverage of the `_dispatch_branch`, `_env_int`, and `_env_float` functions in `src/dispatcher.py`.

## Bug Analysis

No actual bugs were found in the production code during testing. All break-and-revert cycles
introduced artificial defects to demonstrate the test coverage, then restored the original
correct implementation. The production code in `src/dispatcher.py` is functioning as intended.

## Coverage Improvement

**After changes:** 6.7% (2940 statements, 2743 missing)
**Improvement:** 0.2% for dispatcher.py

## Notes

- The full repository baseline coverage is ~80.64% as recorded in config/coverage-baseline.json
- The dispatcher.py module is large (7152 lines) and has extensive uncovered code paths
- This initial test set covers basic utility functions; additional tests would be needed to reach higher coverage
- All tests pass with the original production code (no bugs found)


## Continuation evidence — 2026-11-07

This section supersedes the earlier approximate coverage interpretation and the
unverified assertion that the additional cases followed the same mutation pattern.
All 16 original cases are preserved. The continuation adds 49 cases (17 test
functions), registers this module once in existing shard 1 of 6, and leaves
`src/dispatcher.py` byte-for-byte unchanged. No real production defect was found
in the exercised paths; that is not a claim about the rest of this module.

Acceptance outcome: at least 174 additional uncovered statements/branches reduced,
measured by `/opt/anaconda3/bin/python3 src/verify.py --coverage` (check date
2026-11-07). Stop rule: three failed repair attempts at the same step or 75 minutes.
The focused end-to-end shard registration check passed before adding coverage.
Each focused module run takes under two minutes.

### Measurements and limits

The successful main CI run [42321848468](https://github.com/stranske/Orchestrator/actions/runs/42321848468)
at `c8775621081e0fcd00ee778023d3a6c9cbb670ad` published `gate-coverage/coverage.json`:
`src/dispatcher.py` has 2940 statements, 2774 covered, 166 missing (94.4%).
`git diff c8775621 -- src/dispatcher.py` is empty. The repository config has
`branch = false`; these measurements contain no branch units. The earlier 6.5% /
6.7% figures describe this test module alone, not the combined CI baseline.

The PR's pre-continuation CI run
[42356588672](https://github.com/stranske/Orchestrator/actions/runs/42356588672)
printed `unassigned modules: ['tests/test_dispatcher_coverage_1182.py']` and
`coverage: NOT PUBLISHED, shard 1/6 has no data`. It supplies no combined baseline.

The explicit instruction not to run the full suite locally prevents executing
plain `python3 src/verify.py --coverage` locally: that command runs all pytest
modules, all module selftests, and gates. It was not executed or replaced with a
mocked verification run. Fresh PR CI's aggregate-mode invocation of that verifier
is the authority for the actual combined after result.

Focused measurements were run with:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_dispatcher_coverage_1182.py -q --cov=dispatcher --cov-report=json:/tmp/issue1182-focused-before.json
/opt/anaconda3/bin/python3 -m pytest tests/test_dispatcher_coverage_1182.py -q --cov=dispatcher --cov-report=json:/tmp/issue1182-focused-after.json
```

Before: 16 passed; 2743 missing. After: 65 passed; 2477 missing.
The focused reduction is 266 statements, but only 35 were missing from the combined CI baseline.
Baseline-plus-focused union: 166 -> 131 missing; this is a projected union, not a fresh combined verifier result.
Additional-target shortfall: 139 of 174; total-target shortfall: 145 of 180.
Newly reached baseline-missing statements: 390, 391, 405, 409, 428, 512, 513, 545, 552, 553, 559, 562, 832, 833, 834, 1064, 1065, 1490, 1523, 1524, 1563, 2019, 2024, 2049, 2074, 2080, 2096, 2148, 2558, 2607, 2611, 2618, 2898, 5273, 5274.

Reasons for the shortfall: only 166 statement units existed in the combined
baseline to begin with, and branch measurement is disabled. Most of the remaining
131 missing statements concern controlled-auth/pool admission, provider process
supervision/timeout cleanup, the large offload lifecycle, or selftest-only fallback
paths. These were left unchanged rather than claimed covered by pure helper tests.
`_env_bool` and `_repo_id` do not exist in this revision; existing numeric/prefix
coverage was not padded to claim combined gains.

Hermetic boundary: both `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` point to pytest
scratch state; process and socket calls fail by default and the fixture asserts
none reached that default. Cases needing process evidence use local mocks with
explicit call assertions. No provider or agent was launched.

### Complete break-and-restore runs

Each row is one independently executed pytest node, including every parameter
instance. For each row: apply its catalogued mutation to `src/dispatcher.py`, run
`/opt/anaconda3/bin/python3 -m pytest '<node>' -q`, restore the exact original source,
then run the same command. All failing commands exited 1 and all restored commands
exited 0. Mutations were applied serially and restored in `finally` blocks. The
final source SHA256 is `d27ed8a9d027d58f16703a9b20a2d90b200a2e03850e6e3f9c6f8f88f82ebc12`.
The table records the actual terminal summaries; the mutation catalog below shows
exactly what each case rejected. Parameterized cases were not credited from a
single group run.

| Pytest node | Mutation | Deliberate break output (exit 1) | Restored output (exit 0) |
|---|---|---|---|
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_with_issue_number` | M01 | `1 failed in 0.53s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_with_custom_prefix` | M01 | `1 failed in 0.38s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_without_issue_number` | M01 | `1 failed in 0.35s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_with_trailing_slash_in_prefix` | M01 | `1 failed in 0.39s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_invalid_prefix` | M02 | `1 failed in 0.33s` | `1 passed in 0.33s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranch::test_dispatch_branch_infra_repo` | M01 | `1 failed in 0.33s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::TestEnvInt::test_env_int_default` | M03 | `1 failed in 0.33s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::TestEnvInt::test_env_int_valid` | M03 | `1 failed in 0.34s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::TestEnvInt::test_env_int_invalid` | M04 | `1 failed in 0.34s` | `1 passed in 0.36s` |
| `tests/test_dispatcher_coverage_1182.py::TestEnvFloat::test_env_float_default` | M05 | `1 failed in 0.35s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::TestEnvFloat::test_env_float_valid` | M05 | `1 failed in 0.34s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::TestEnvFloat::test_env_float_invalid` | M06 | `1 failed in 0.35s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranchEdgeCases::test_dispatch_branch_agent_prefix` | M01 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranchEdgeCases::test_dispatch_branch_hyphenated_prefix` | M01 | `1 failed in 0.34s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranchEdgeCases::test_dispatch_branch_underscore_prefix` | M01 | `1 failed in 0.34s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::TestDispatchBranchEdgeCases::test_dispatch_branch_mixed_chars_prefix` | M01 | `1 failed in 0.35s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::test_prompt_without_template_keeps_owner_decisions` | M07 | `1 failed in 0.34s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_capability_matching_failure_does_not_block_prompt` | M08 | `1 failed in 0.49s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_unbounded_git_probe_timeout_is_not_a_worktree` | M09 | `1 failed in 0.34s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_partial_cleanup_requires_the_original_inode[False]` | M10 | `1 failed in 0.34s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_partial_cleanup_requires_the_original_inode[True]` | M11 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_copier_terminal_requires_absent_group[running]` | M12 | `1 failed in 0.34s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_copier_terminal_requires_absent_group[absent]` | M13 | `1 failed in 0.34s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_copier_terminal_requires_absent_group[unreadable]` | M14 | `1 failed in 0.34s` | `1 passed in 0.53s` |
| `tests/test_dispatcher_coverage_1182.py::test_copier_terminal_requires_absent_group[denied]` | M15 | `1 failed in 0.36s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_copier_terminal_requires_absent_group[alive]` | M16 | `1 failed in 0.34s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_workspace_copy_refuses_before_creating_output[False]` | M17 | `1 failed in 0.33s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_workspace_copy_refuses_before_creating_output[True]` | M18 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_vibe_config_replaces_only_session_logging_save_dir` | M19 | `1 failed in 0.33s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_guard_cleanup_error_preserves_original_write_error` | M20 | `1 failed in 0.51s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_compiled_capability_refuses_incomplete_evidence[lineage]` | M21 | `1 failed in 0.37s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_compiled_capability_refuses_incomplete_evidence[lookup]` | M22 | `1 failed in 0.36s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_compiled_capability_refuses_incomplete_evidence[missing-binding]` | M22 | `1 failed in 0.35s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_compiled_capability_refuses_incomplete_evidence[kind]` | M22 | `1 failed in 0.34s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_compiled_capability_refuses_incomplete_evidence[invoke]` | M22 | `1 failed in 0.35s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_compiled_capability_refuses_incomplete_evidence[receipt]` | M22 | `1 failed in 0.38s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[exit]` | M23 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[pages]` | M23 | `1 failed in 0.36s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[rest-page]` | M23 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[body]` | M23 | `1 failed in 0.34s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[pagination]` | M23 | `1 failed in 0.34s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[cursor]` | M23 | `1 failed in 0.35s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[nodes]` | M23 | `1 failed in 0.33s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[refs]` | M23 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_read_keeps_malformed_evidence_unknown[repo]` | M23 | `1 failed in 0.32s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::test_controlled_source_digest_requires_bounded_file[None]` | M24 | `1 failed in 0.48s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::test_controlled_source_digest_requires_bounded_file[empty]` | M24 | `1 failed in 0.40s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_controlled_source_digest_requires_bounded_file[large]` | M24 | `1 failed in 0.47s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_controlled_source_digest_requires_bounded_file[valid]` | M25 | `1 failed in 0.33s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_controlled_cleanup_does_not_retry_an_uncertain_removal` | M26 | `1 failed in 0.35s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_public_observation_hides_raw_argv[None]` | M27 | `1 failed in 0.33s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_public_observation_hides_raw_argv[observation1]` | M28 | `1 failed in 0.33s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_remote_label_success_survives_feedback_failure` | M29 | `1 failed in 0.44s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_number_must_be_a_positive_integer[True]` | M23 | `1 failed in 0.34s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_number_must_be_a_positive_integer[0]` | M23 | `1 failed in 0.36s` | `1 passed in 0.47s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_number_must_be_a_positive_integer[-1]` | M23 | `1 failed in 0.41s` | `1 passed in 0.34s` |
| `tests/test_dispatcher_coverage_1182.py::test_linked_pr_number_must_be_a_positive_integer[2]` | M23 | `1 failed in 0.36s` | `1 passed in 0.32s` |
| `tests/test_dispatcher_coverage_1182.py::test_finite_pool_fence_rejects_untrusted_identity[extra0]` | M30 | `1 failed in 0.35s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_finite_pool_fence_rejects_untrusted_identity[extra1]` | M30 | `1 failed in 0.36s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_finite_pool_fence_rejects_untrusted_identity[extra2]` | M30 | `1 failed in 0.34s` | `1 passed in 0.34s` |
| `tests/test_dispatcher_coverage_1182.py::test_invalid_review_round_refuses_before_claiming[True]` | M31 | `1 failed in 0.34s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_invalid_review_round_refuses_before_claiming[1]` | M31 | `1 failed in 0.34s` | `1 passed in 0.29s` |
| `tests/test_dispatcher_coverage_1182.py::test_invalid_review_round_refuses_before_claiming[0]` | M31 | `1 failed in 0.34s` | `1 passed in 0.31s` |
| `tests/test_dispatcher_coverage_1182.py::test_invalid_review_round_refuses_before_claiming[-1]` | M31 | `1 failed in 0.34s` | `1 passed in 0.30s` |
| `tests/test_dispatcher_coverage_1182.py::test_invalid_review_round_refuses_before_claiming[1000000]` | M31 | `1 failed in 0.34s` | `1 passed in 0.30s` |

### Mutation catalog

#### M01

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -209,5 +209,5 @@
     if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*", prefix):
         raise ValueError("branch prefix must contain nonempty alphanumeric, '_' or '-' segments")
-    return f"{prefix}/issue-{num}" if num is not None else f"{prefix}/{lane}"
+    return "wrong-branch"


```

#### M02

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -207,5 +207,5 @@
     prefix = prefix if prefix is not None else ("orch" if repo in INFRA_REPOS else agent)
     prefix = prefix.rstrip("/")
-    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*", prefix):
+    if False and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*", prefix):
         raise ValueError("branch prefix must contain nonempty alphanumeric, '_' or '-' segments")
     return f"{prefix}/issue-{num}" if num is not None else f"{prefix}/{lane}"
```

#### M03

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -214,5 +214,5 @@
 def _env_int(name: str, default: int) -> int:
     try:
-        return int(os.environ.get(name, str(default)))
+        return int(os.environ.get(name, str(default))) + 1
     except (TypeError, ValueError):
         return default
```

#### M04

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -216,5 +216,5 @@
         return int(os.environ.get(name, str(default)))
     except (TypeError, ValueError):
-        return default
+        return default + 1


```

#### M05

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -221,5 +221,5 @@
 def _env_float(name: str, default: float) -> float:
     try:
-        return float(os.environ.get(name, str(default)))
+        return float(os.environ.get(name, str(default))) + 1
     except (TypeError, ValueError):
         return default
```

#### M06

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -223,5 +223,5 @@
         return float(os.environ.get(name, str(default)))
     except (TypeError, ValueError):
-        return default
+        return default + 1


```

#### M07

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -407,5 +407,5 @@
     tmpl = PROMPT_TEMPLATES.get(task_type)
     if not tmpl:
-        body = f"Work {target} ({task_type}). {target_detail}".strip()
+        body = "lost owner context"
     else:
         body = tmpl.format(target=target, target_detail=target_detail).strip()
```

#### M08

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -389,5 +389,5 @@
         )
     except Exception:
-        pass
+        raise


```

#### M09

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -426,5 +426,5 @@
                 "owned request window expired checking workspace provenance"
             ) from None
-        return False
+        return True
     return res.returncode == 0 and res.stdout.strip() == "true"

```

#### M10

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -511,5 +511,5 @@
         current = partial.lstat()
     except FileNotFoundError:
-        return True
+        return False
     if partial.is_symlink() or (current.st_dev, current.st_ino) != identity:
         return False
```

#### M11

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -513,5 +513,5 @@
         return True
     if partial.is_symlink() or (current.st_dev, current.st_ino) != identity:
-        return False
+        return True
     cleanup = (
         "import os,shutil,stat,sys\n"
@@ -537,5 +537,5 @@
         with contextlib.suppress(subprocess.TimeoutExpired):
             child.communicate(timeout=2)
-        return False
+        return True
     return child.returncode == 0 and not partial.exists() and not partial.is_symlink()

```

#### M12

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -543,5 +543,5 @@
 def _offload_copier_terminal(proc) -> bool:
     if proc.poll() is None:
-        return False
+        return True
     try:
         os.killpg(proc.pid, 0)
```

#### M13

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -547,5 +547,5 @@
         os.killpg(proc.pid, 0)
     except ProcessLookupError:
-        return True
+        return False
     except PermissionError:
         raise
```

#### M14

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -551,5 +551,5 @@
         raise
     except OSError:
-        return False
+        return True
     return False

```

#### M15

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -549,5 +549,5 @@
         return True
     except PermissionError:
-        raise
+        return False
     except OSError:
         return False
```

#### M16

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -543,5 +543,5 @@
 def _offload_copier_terminal(proc) -> bool:
     if proc.poll() is None:
-        return False
+        return True
     try:
         os.killpg(proc.pid, 0)
@@ -551,6 +551,6 @@
         raise
     except OSError:
-        return False
-    return False
+        return True
+    return True


```

#### M17

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -560,5 +560,5 @@
     src = adapters.workspace_path(cwd)
     if not src.is_dir():
-        raise FileNotFoundError(f"offload cwd is not a directory: {src}")
+        return src
     OFFLOAD_DIR.mkdir(parents=True, exist_ok=True)
     stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
```

#### M18

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -557,5 +557,5 @@
 def _isolate_offload_cwd(cwd: str | Path, *, timeout: float | None = None) -> Path:
     if timeout is not None and timeout <= 0:
-        raise TimeoutError("bounded offload workspace copy has no request time remaining")
+        return Path(cwd)
     src = adapters.workspace_path(cwd)
     if not src.is_dir():
```

#### M19

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -829,5 +829,5 @@
             in_session_logging = stripped == "[session_logging]"
             saw_section = saw_section or in_session_logging
-        if in_session_logging and stripped.startswith("save_dir"):
+        if False and in_session_logging and stripped.startswith("save_dir"):
             out.append(f'save_dir = "{session_dir}"')
             saw_save_dir = True
```

#### M20

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -1063,5 +1063,5 @@
                 os.unlink(tmp)
             except OSError:
-                pass
+                raise
             raise
     return guard
```

#### M21

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -1488,5 +1488,5 @@
     if cap_id in ids:
         if versions and len(ids) != len(versions):
-            raise ValueError("capability identity/version lineage length mismatch")
+            return None
         d["capability_ids"] = [cid for cid in ids if cid != cap_id]
         d["capability_version_ids"] = [
@@ -1532,5 +1532,5 @@
         versions = [str((ledger.get(cid) or {}).get("capability_version_id") or "") for cid in ids]
     if len(ids) != len(versions):
-        raise ValueError("capability identity/version lineage length mismatch")
+        return None
     missing_versions = [cid for cid, version in zip(ids, versions) if cid != cap_id and not version]
     if missing_versions:
```

#### M22

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -1495,5 +1495,5 @@

     def fallback(reason: str, *, invoked: bool = False) -> dict:
-        result = {"invoked": invoked, "accepted": False, "reason": reason}
+        result = {"invoked": invoked, "accepted": True, "reason": reason}
         routing["capability_dispatch_result"] = result
         routing["capability_fallback"] = "baseline"
```

#### M23

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2001,5 +2001,5 @@
     if not match:
         _linked_pr_read_error.set("expected owner/repo#N")
-        return None
+        return []
     owner, name, number = match.groups()
     repo = f"{owner}/{name}"
@@ -2109,5 +2109,5 @@
     ) as exc:
         _linked_pr_read_error.set(str(exc))
-        return None
+        return []


```

#### M24

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2605,9 +2605,9 @@
     source = getattr(module, "__file__", None)
     if not isinstance(source, str):
-        raise ValueError("controlled source module identity unavailable")
+        raise RuntimeError("controlled source module identity unavailable")
     with Path(source).open("rb") as stream:
         raw = stream.read(8_000_001)
     if not raw or len(raw) > 8_000_000:
-        raise ValueError("controlled source module exceeds bound")
+        raise RuntimeError("controlled source module exceeds bound")
     return hashlib.sha256(raw).hexdigest()

```

#### M25

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2610,5 +2610,5 @@
     if not raw or len(raw) > 8_000_000:
         raise ValueError("controlled source module exceeds bound")
-    return hashlib.sha256(raw).hexdigest()
+    return "wrong-digest"


```

#### M26

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2615,5 +2615,5 @@
 def _controlled_cleanup_result(context: _ControlledDataOnly, result: dict) -> None:
     """Finalize owned filesystem cleanup before immutable capture retention."""
-    if context.runtime_cleanup_attempted:
+    if False and context.runtime_cleanup_attempted:
         return
     context.runtime_cleanup_attempted = True
```

#### M27

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2556,5 +2556,5 @@
     """Preserve process evidence without persisting raw prompt-bearing argv."""
     if not isinstance(observation, dict):
-        return None
+        return {}
     public = {k: v for k, v in observation.items() if k not in {"provider_argv", "transport_argv"}}
     for name in ("provider_argv", "transport_argv"):
```

#### M28

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2565,5 +2565,5 @@
                 b"".join(os.fsencode(arg) + b"\0" for arg in argv)
             ).hexdigest()
-    return public
+    return observation


```

#### M29

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -5272,5 +5272,5 @@
         )
     except Exception:
-        pass
+        raise
     return {
         "target": target,
```

#### M30

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2880,5 +2880,5 @@
     """Return only trusted process identity for an unverified finite cleanup."""
     if not isinstance(observation, dict) or observation.get("cleanup_terminal") is not False:
-        return None
+        return {}
     pgid = observation.get("process_group", observation.get("pid"))
     if (
@@ -2896,5 +2896,5 @@
         or not observation.get("finite_frontier_target")
     ):
-        return None
+        return {}
     return {
         "process_group": pgid,
```

#### M31

```diff
--- src/dispatcher.py
+++ deliberate mutation
@@ -2146,5 +2146,5 @@
             or not 1 <= review_round <= REVIEW_LOOP_MAX_ROUNDS
         ):
-            return {"error": "review round outside bounded review loop"}
+            return {}
     explicit_mode = mode
     if mode is None:
```
