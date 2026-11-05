# Issue #1109: PR #793 acceptance evidence

Companion to `docs/verification/issue-1109-synthesis-promotion-acceptance.md`.
That file is the primary record. This file adds the in-process rebind of cached
runtime roots and a local `--branch` measurement. The revisions, CI runs, 26-statement
reduction, and 154-unit shortfall agree.

Check date: 2026-11-05. This document records acceptance evidence for merged PR #793
(`closer-core/v1 followup-of=stranske/Orchestrator#791@793`). It does not replace the tests
PR #793 added.

Measurements below are labeled **historical CI artifact** or **local partial**. This session
did not start a new CI run. Historical results are not a new passing CI run.

## Identification

`docs/AGENT_ISSUE_FORMAT.md` defines the Tasks and Acceptance Criteria checkbox contract used
by issue #1109.

| Fact | Value |
| --- | --- |
| PR | https://github.com/stranske/Orchestrator/pull/793 |
| Merge revision | `574c1694b2990e50ce0668589472e180f481019c` |
| Merge subject | `test: cover synthesis promotion safety paths (#793)` |
| Parent revision | `2f12281b795d749498e5641edcf32ef41420f499` |
| Parent subject | `chore(floor): reconcile the collection floor on main` |
| Parent vs its parent | `.verify-floor.json` only (`08c7b70a8f38af8217bdf1dd8d8ce9adad6ac4af`) |
| PR head with the passing pytest job | `891a157836ac6556bbecd4dc192680cb5b9c3933` |
| Production module | `src/synthesis_promotion.py` |
| Added test file | `tests/test_synthesis_promotion_coverage.py` (131 lines at the merge) |

The three tests added by the merge, and their pytest node IDs:

- `tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head`
- `tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command`
- `tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning`

Symbols they exercise in `src/synthesis_promotion.py`: `default_completion_probe` (the
completion probe), `_run_gate`, and `verify_synthesis`. The merge diff touches only the test
file. Production code did not change in PR #793.

## Original coverage acceptance contract

Issue #791 (coverage-autopilot round 7, lane C) is the contract PR #793 was judged on:

- Uncovered units come from the whole CI run. `python3 src/verify.py --coverage` is the
  combined pytest-plus-selftest measure. The PR's CI coverage artifact is the authority.
  A plain `pytest --cov` run reads lower because it omits the other tests and the selftests.
- One PR covers the next ranked targets until about 180 uncovered units are covered, or the
  list is exhausted. A partial PR meets the criterion when it states the shortfall.
- Rank order is escaped defects, then churn, then uncovered mass. The issue's published
  targets were `src/synthesis_promotion.py` (168 uncovered, 3 escaped-defect signals, churn 6,
  a fix commit touched the file) then `src/codemod_lane.py` (123 uncovered, 3 escaped-defect
  signals, churn 6). Reserved files, including `src/dispatcher.py` and `src/capabilities.py`,
  were out of scope.
- At the merge revision, `pyproject.toml` sets `[tool.coverage.run] source = ["src"]`,
  `branch = false`, and `parallel = true`. The issue's phrase "statements+branches" matches
  the statement miss count from that configuration. The artifact has no branch counters.

The CI command that produced the authoritative payload, from `.github/workflows/ci.yml` at
the merge revision, job `combined coverage (pytest + selftests, the honest number)`:

```
python3 src/verify.py --coverage --floor-may-lag
```

That job publishes artifacts named `gate-coverage` (`coverage.json`) and
`gate-coverage-trend` (`coverage-trend.json`). The Gate workflow downloads those CI
artifacts. It does not remeasure the tree. Gate runs for this PR published no artifacts of
their own.

## CI pytest and coverage jobs

Historical only. No new CI run was started for this evidence.

| Role | Revision | Run | Job | Command | Result |
| --- | --- | --- | --- | --- | --- |
| Pre-addition combined coverage | `08c7b70a8f38af8217bdf1dd8d8ce9adad6ac4af` | [39254310194](https://github.com/stranske/Orchestrator/actions/runs/39254310194) | combined coverage, id `118065628727` | `python3 src/verify.py --coverage --floor-may-lag` | success; artifact `gate-coverage` id `12187861769` |
| Pre-addition pytest verdict | same | same run | `verify.py (real pytest counts + selftests + capability gates)`, id `118065629042` | `python3 src/verify.py` (push) | **job failure**. This is not a passing pytest result for the pre-addition revision. |
| Passing CI pytest | `891a157836ac6556bbecd4dc192680cb5b9c3933` | [39260265878](https://github.com/stranske/Orchestrator/actions/runs/39260265878) | verify.py, id `118085764899` | `python3 src/verify.py --floor-may-lag` (pull_request) | **job success**. Log: `pytest: 5791 passed, 0 failed, 27/31 max skipped (5818 collected; floor 5815 — 3 BEHIND)`. The floor lag was printed and was not fatal on the pull request. |
| PR combined coverage | same PR head | same run | combined coverage, id `118085764499` | `python3 src/verify.py --coverage --floor-may-lag` | success; artifact `gate-coverage` id `12191166790`, trend id `12191311822` |
| Merge pytest verdict | `574c1694b2990e50ce0668589472e180f481019c` | [39270663547](https://github.com/stranske/Orchestrator/actions/runs/39270663547) | verify.py, id `118121764897` | `python3 src/verify.py` (push, no `--floor-may-lag`) | **job failure**. Log: `pytest: 5791 passed, 0 failed`. The failure is the collection floor (`5818 collected > floor 5815`), not a failed test. |
| Merge combined coverage | same merge revision | same run | combined coverage, id `118121764711` | `python3 src/verify.py --coverage --floor-may-lag` | success; artifact `gate-coverage` id `12194284584`, trend id `12194933485` |
| PR Gate | PR head | [39260266708](https://github.com/stranske/Orchestrator/actions/runs/39260266708) | Gate workflow | downloads CI `gate-coverage` when present | workflow success; **0 artifacts published by this run** |
| Merge Gate | merge revision | [39270664385](https://github.com/stranske/Orchestrator/actions/runs/39270664385) | Gate workflow | same | workflow success; **0 artifacts published by this run** |

Unavailable historical evidence, distinct from the passing CI pytest job above:

- Parent revision `2f12281b795d749498e5641edcf32ef41420f499` has no `CI` workflow run.
  Its tree differs from `08c7b70` only in `.verify-floor.json`, so the pre-addition
  `gate-coverage` artifact is the production measurement for that parent.
- The merge revision's verify.py job failed. Its pytest count passed. It is not the
  passing CI pytest result named above.
- The pre-addition verify.py job also failed. No passing pytest job was found for
  `08c7b70` or `2f12281`.
- Gate runs did not retain a separate coverage payload. The CI `gate-coverage` artifacts
  are the authoritative payloads, and they were still downloadable on 2026-11-05
  (`expired: false`).
- No new CI run was executed for this follow-up. The passing result is the historical
  pull-request job on `891a157`.

`coverage.json` `meta.branch_coverage` is `false` in the pre-addition, PR-head, and merge
artifacts. Totals have statement fields only (`missing_lines`, `covered_lines`,
`num_statements`). `num_branches` is absent.

## Authoritative before/after coverage

Same configuration (`branch = false`, `source = ["src"]`, `verify.py --coverage`) and the
same production file. **Historical CI artifact**, not a local re-run of `verify.py`.

| Revision | `src/synthesis_promotion.py` uncovered statements | Covered statements | Statements | Uncovered branches |
| --- | --- | --- | --- | --- |
| Pre-addition `08c7b70` (parent production) | 168 | 816 | 984 | unmeasured (`branch_coverage: false`) |
| PR head `891a157` | 142 | 842 | 984 | unmeasured |
| Merge `574c1694` | 142 | 842 | 984 | unmeasured |

`src/codemod_lane.py` stays at 123 uncovered statements in all three artifacts. PR #793
did not change it. The PR body said the first target was not fully exhausted.

Lines present in the pre-addition missing set and absent from the merge missing set
(26 statements): 468-482, 484-487, 527, 533, 623, 628, 673-675. No statement became
newly missing. Whole-tree `missing_lines` moved from 9981 (`08c7b70`) to 9956 (PR head)
to 9954 (merge). The module-level synthesis delta is 26 at both the PR head and the
merge; the extra whole-tree statement between those two artifacts is outside this module.

Achieved reduction under the original contract: **26 uncovered statements**. Uncovered
branches in that contract's artifact: **unmeasured**. Combined reduction of the published
168-unit figure: **26**. Shortfall against approximately 180 units: **154**
(`180 - 26`). Covering the remaining 123 statements in `src/codemod_lane.py` would still
leave the chunk short of 180 (`26 + 123 = 149`).

Target limitations of the authoritative result:

- Branch coverage is disabled, so a statements-plus-branches sum cannot be read from the
  Gate payload. Treating the issue's 168 as that sum double-counts the statement miss.
- 142 statements in `src/synthesis_promotion.py` remain uncovered after the three tests.
- The second ranked target was not attempted.
- `verify.py --coverage` was not re-run locally. The numbers above are the historical
  artifacts.

## Local partial coverage

**Local partial**, same `pyproject.toml` source scope (`--source=src`) and, for the
statement runs, the same `branch = false` default. These runs execute only the synthesis
promotion test modules. They omit the rest of pytest and every module `--selftest`, so
the miss counts are higher than CI and are not the contract result.

Interpreter: `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3` (Python 3.14.9).
`/opt/anaconda3/bin/python3` is not on this machine. Pytest was installed into that
interpreter for the measurement (pytest 9.2.0, coverage 7.17.2).

Statement command at `/tmp/orch-1109-before` (`2f12281`), with both runtime variables set
to fresh temporary directories:

```
python3 -m coverage run --source=src -m pytest \
  tests/test_synthesis_promotion.py tests/test_synthesis_delivery_discovery.py \
  -m "not slow" -q -p no:cacheprovider
python3 -m coverage report --include='*/synthesis_promotion.py'
```

The merge worktree `/tmp/orch-1109-after` (`574c1694`) used the same command plus
`tests/test_synthesis_promotion_coverage.py`. Branch runs added `--branch`, which
overrides `branch = false` and is therefore **not** the Gate configuration.

| Local run | Uncovered statements | Uncovered branches | Partial branches | Passed |
| --- | --- | --- | --- | --- |
| Before, `branch = false` | 490 of 984 | unmeasured | unmeasured | 79 |
| After, `branch = false` | 461 of 984 | unmeasured | unmeasured | 82 |
| Before, `--branch` | 490 of 984 | 186 of 328 | 58 | 79 |
| After, `--branch` | 461 of 984 | 172 of 328 | 54 | 82 |

Local statement reduction on this partial suite: **29** (`490 - 461`). Three of those
statements were already covered by the rest of CI, which is why the authoritative
reduction is 26 rather than 29. Local uncovered-branch reduction under `--branch`: **14**
(`186 - 172`). That 14 is not added to the contract reduction. A partial-suite sum of
29 statements and 14 branches would still be 137 short of 180, and it uses a
configuration the Gate does not run.

## Target-selection and ranking rationale

The issue #791 text is the selection snapshot: take `src/synthesis_promotion.py` first
because the autopilot reported 168 uncovered units, 3 escaped-defect signals, churn 6,
and a fix commit touching the file; then `src/codemod_lane.py` at 123 / 3 / 6. Reserved
files were excluded. PR #793 followed that order, covered part of the first file, and
stopped before the second. Its body left the after value to Gate and reported a local
`pytest --cov` figure of 181/984 (18.4%) as not comparable to CI.

Reproduced on 2026-11-05 against the parent worktree and the pre-addition artifact
(command below). `uncovered_by_file` returns 168 for `src/synthesis_promotion.py` and
123 for `src/codemod_lane.py`, matching the issue's unit and the artifact. `rank` on
that worktree, lookback 180, returns `rank_key [3.0, 11.0, 168.0]` for synthesis
(escaped 3.0, churn 11, uncovered 168, evidence `fix commit touched this file`) and
`[5.0, 9.0, 123.0]` for codemod. After dropping the issue's reserved paths, synthesis
is eligible position 48, not 1. The reproduced escaped count matches the issue's "3".
The reproduced churn does not match the issue's "6", and the fresh order does not place
synthesis first. The contract order remains the order issue #791 published. This
reproduction explains the uncovered-mass input and shows the limit of re-ranking later.

```
python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, "src")
from escaped_defect_priority import rank, uncovered_by_file
before = json.loads(Path("/tmp/cov-793/before/coverage.json").read_text())
print(uncovered_by_file(before)["src/synthesis_promotion.py"])
print(uncovered_by_file(before)["src/codemod_lane.py"])
rows = rank("/tmp/orch-1109-before", before, lookback_days=180, limit=400)
PY
```

The artifact file was `gh run download 39254310194 -n gate-coverage`.

## Runtime initialization

At the merge revision, importing `src/synthesis_promotion.py` executes
`import capabilities`, `import feedback`, and `import local_verify` (lines 29-31).
Those imports bind runtime paths before any test body:

- `src/feedback.py:48` sets `LOCAL_RUNTIME` from `ORCH_LOCAL_RUNTIME`, defaulting to
  `~/.codex/orchestrator`.
- `src/feedback.py:51` and `:103` set `DB_PATH` from `ORCH_FEEDBACK_DB` or from
  `resolve_db_path()`. `resolve_db_path` uses `ORCH_LOCAL_RUNTIME` when it is set, and
  uses `ORCH_STATE_DIR` only when both `ORCH_FEEDBACK_DB` and `ORCH_LOCAL_RUNTIME` are
  unset.
- `src/capabilities.py:34` and `:38` set `LOCAL_RUNTIME` and `REG` the same way.

`synthesis_promotion` itself has no `ORCH_STATE_DIR` read. The current tree still binds
the same two modules at import and also imports `runtime_ac`, which imports `feedback`
again. A pytest module that imports `synthesis_promotion` at collection time binds those
paths before a fixture can call `monkeypatch.setenv`. Setting the variables later does
not move `feedback.LOCAL_RUNTIME`, `feedback.DB_PATH`, `capabilities.LOCAL_RUNTIME`, or
`capabilities.REG`.

`tests/test_synthesis_promotion_coverage.py` now does both of the following:

1. The autouse fixture sets `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` to directories
   under pytest's `tmp_path`, clears `ORCH_FEEDBACK_DB` and `ORCH_CAPABILITIES_PATH`,
   and rebinds the cached attributes before the test body. It asserts both environment
   values and both cached roots are those temporary directories, and that the database
   and ledger paths are not under `~/.codex/orchestrator`.
2. `test_fresh_import_binds_runtime_paths_before_initialization` starts a new
   interpreter. The child checks that `synthesis_promotion`, `feedback`, `capabilities`,
   `runtime_ac`, and `local_verify` are absent from `sys.modules`, then imports
   `synthesis_promotion`. `feedback.LOCAL_RUNTIME` and `capabilities.LOCAL_RUNTIME`
   equal the temporary runtime directory. `DB_PATH` and `REG` resolve inside it.
   Assigning the environment variables again after import leaves the cached roots on
   the pre-import temporary directory.

That fresh-import test passed in the baseline command below.

## Baseline pytest

Selected interpreter: `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3`
(Python 3.14.9). `/opt/anaconda3/bin/python3` is absent, so it was not used.
Working-tree revision at the run: `612dc68374be9eefcc03db3f909e59eaba020644`.
`src/synthesis_promotion.py` matched that revision. The test module had uncommitted
isolation edits.

```
ORCH_STATE_DIR=/tmp/tmp.HSXY19eW6l ORCH_LOCAL_RUNTIME=/tmp/tmp.vS6vmfkzgb \
  /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 -m pytest -q \
  tests/test_synthesis_promotion_coverage.py -m "not slow" --tb=line
```

Result: `4 passed` in 0.25s (the three PR #793 tests plus the fresh-import test).

## Deliberate mutations

Each mutation was applied to `src/synthesis_promotion.py` on revision `612dc683`,
the named node was run, the file was restored from a saved copy, and `git diff --
src/synthesis_promotion.py` was empty (`0` lines) before the next mutation. Runtime
directories: `ORCH_STATE_DIR=/tmp/tmp.QuxOBw1RH0` and
`ORCH_LOCAL_RUNTIME=/tmp/tmp.3Z6RZTOhAh`.

Command shape for each node:

```
/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 -m pytest <node> \
  -m "not slow" --tb=short -q -p no:cacheprovider
```

| Test | Mutation | Named failure | After restore |
| --- | --- | --- | --- |
| `test_completion_probe_requires_a_success_marker_and_new_head` | `default_completion_probe`: `if rc != 0:` changed to `if rc == 0:` | `AssertionError` on that node: status `interrupted` / `synthesis marker rc=0` instead of `complete` | included in the restored run |
| `test_run_gate_never_runs_non_allowlisted_command` | `_run_gate`: `if not _safe_command(argv):` changed to `if _safe_command(argv):` | `AssertionError: subprocess must not run an unallowlisted gate` | included in the restored run |
| `test_verify_synthesis_records_unallowlisted_local_command_without_spawning` | `verify_synthesis`: `if not _safe_command(local_argv):` changed to `if _safe_command(local_argv):` | `AssertionError: rejected command must not reach local verification` | included in the restored run |

Restored command, same interpreter and the same two temporary runtime directories:

```
/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 -m pytest \
  tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head \
  tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command \
  tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning \
  -m "not slow" --tb=short -q -p no:cacheprovider
```

Result: `3 passed` in 0.13s. No deliberate production mutation remains.

## Historical gaps carried forward

- Anthropic evaluation recorded on the source follow-up did not run (HTTP 400,
  insufficient credit, request `req_011CgBmxtRBHwybgfedRGXVe`). That is unavailable
  evaluation, not corroboration. This evidence does not depend on it.
- PR #793's own body documented a break-and-revert only for the completion-probe test,
  and it deferred the after-coverage number to Gate. The three mutations and the
  artifact counts in this document are the missing records.
- The passing CI pytest revision is the PR head `891a157`, not the merge commit.
