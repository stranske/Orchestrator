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
artifacts. It does not remeasure the tree. Gate-run files were unavailable at inspection. Zero currently listed artifacts
does not establish non-upload: the Gate workflow uses seven-day retention, and
no historical upload logs were established here.

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
| PR Gate | PR head | [39260266708](https://github.com/stranske/Orchestrator/actions/runs/39260266708) | Gate workflow | downloads CI `gate-coverage` when present | workflow success; **0 artifacts currently listed; upload history unestablished** |
| Merge Gate | merge revision | [39270664385](https://github.com/stranske/Orchestrator/actions/runs/39270664385) | Gate workflow | same | workflow success; **0 artifacts currently listed; upload history unestablished** |

Unavailable historical evidence, distinct from the passing CI pytest job above:

- Parent revision `2f12281b795d749498e5641edcf32ef41420f499` has no `CI` workflow run.
  Its tree differs from `08c7b70` only in `.verify-floor.json`, so the pre-addition
  `gate-coverage` artifact is the production measurement for that parent.
- The merge revision's verify.py job failed. Its pytest count passed. It is not the
  passing CI pytest result named above.
- The pre-addition verify.py job also failed. No passing pytest job was found for
  `08c7b70` or `2f12281`.
- A separate Gate coverage payload is unavailable at inspection. The CI `gate-coverage` artifacts
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
the miss counts are higher than CI and are not the contract result. Unlike local suites,
CI artifacts are labeled separately as comparable historical CI artifacts.

Interpreter: `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3` (Python 3.14.9).
`/opt/anaconda3/bin/python3` is not on this machine. Pytest was installed into that
interpreter for the measurement (pytest 9.2.0, coverage 7.17.2).

The earlier command sketch did not isolate parallel files or export the after JSON;
it was incomplete and is superseded by the [clean sequence below](#clean-local-coverage-sequence).
The following table retains the earlier local results and interpreter provenance.
The new sequence uses branch instrumentation and remains separate from branch-disabled
historical CI evidence.

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
that worktree, lookback 180, returns the two target rows: synthesis promotion with
`rank_key [3.0, 11.0, 168.0]` (escaped 3.0, churn 11, uncovered 168, evidence
`fix commit touched this file`) and codemod with `[5.0, 9.0, 123.0]`. After explicitly
filtering the issue's reserved paths (`src/dispatcher.py`, `src/capabilities.py`), synthesis
is at eligible position 48, not 1. The reproduced escaped count matches the issue's "3".
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
print("synthesis_promotion:", uncovered_by_file(before)["src/synthesis_promotion.py"])
print("codemod_lane:", uncovered_by_file(before)["src/codemod_lane.py"])
rows = rank("/tmp/orch-1109-before", before, lookback_days=180, limit=400)
reserved = {"src/dispatcher.py", "src/capabilities.py"}
eligible = [r for r in rows if r["path"] not in reserved]
for r in eligible:
    if r["path"] in ("src/synthesis_promotion.py", "src/codemod_lane.py"):
        print(r["path"], "rank_key", r["rank_key"], "eligible_pos", eligible.index(r) + 1)
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

## Historical baseline pytest

Historically selected interpreter: `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3`
(Python 3.14.9). `/opt/anaconda3/bin/python3` is absent, so it was not used.
Working-tree revision at the run: `612dc68374be9eefcc03db3f909e59eaba020644`.
`src/synthesis_promotion.py` matched that revision. The test module had uncommitted
isolation edits.

Historical command restored verbatim from commit `7860c3a7e63890d11556757311e8905b6a4084ad`.
That revision's test module contains all four tests, including the fresh-import test;
the later five-test selector was incorrectly attached to this four-test result.
This is a historical record, not a command re-executed on this continuation:

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

Command shape for each node (runtime directories set before process startup):

```
ORCH_STATE_DIR=/tmp/tmp.QuxOBw1RH0 ORCH_LOCAL_RUNTIME=/tmp/tmp.3Z6RZTOhAh \
  /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 -m pytest <node> \
  -m "not slow" --tb=short -q -p no:cacheprovider
```

| Test | Mutation | Named failure | After restore |
| --- | --- | --- | --- |
| `test_completion_probe_requires_a_success_marker_and_new_head` | `default_completion_probe`: `if rc != 0:` changed to `if rc == 0:` | `AssertionError` on that node: status `interrupted` / `synthesis marker rc=0` instead of `complete` | included in the restored run |
| `test_run_gate_never_runs_non_allowlisted_command` | `_run_gate`: `if not _safe_command(argv):` changed to `if _safe_command(argv):` | `AssertionError: subprocess must not run an unallowlisted gate` | included in the restored run |
| `test_verify_synthesis_records_unallowlisted_local_command_without_spawning` | `verify_synthesis`: `if not _safe_command(local_argv):` changed to `if _safe_command(local_argv):` | `AssertionError: rejected command must not reach local verification` | included in the restored run |

Restored command, same interpreter and the same two temporary runtime directories (set before process startup):

```
ORCH_STATE_DIR=/tmp/tmp.QuxOBw1RH0 ORCH_LOCAL_RUNTIME=/tmp/tmp.3Z6RZTOhAh \
  /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 -m pytest \
  tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head \
  tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command \
  tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning \
  -m "not slow" --tb=short -q -p no:cacheprovider
```

Result: `3 passed` in 0.13s. No deliberate production mutation remains.

## Historical gaps carried forward

- Anthropic evaluation recorded on the source follow-up did not run (HTTP 400,
  insufficient credit, request `req_011CgBmxtRBHwybgfedRGXVe`). That evaluation is
  unavailable at inspection, not corroboration. This evidence does not depend on it.
- PR #793's own body documented a break-and-revert only for the completion-probe test,
  and it deferred the after-coverage number to Gate. The three mutations and the
  artifact counts in this document are the missing records.
- The passing CI pytest revision is the PR head `891a157`, not the merge commit.

## Historical continuation verification run (eight tests)

Repository-selected interpreter: `/Users/teacher/.codex/py314/bin/python` (Python 3.14.3).
`/opt/anaconda3/bin/python3` is present (Python 3.12.2).

Revision at run: `7860c3a7e63890d11556757311e8905b6a4084ad`.
`git diff -- src/synthesis_promotion.py` is empty (0 lines).

Fresh temporary runtime paths: `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` set to separate
`mktemp -d` directories before process startup, plus `TMPDIR` to avoid `.codex/orchestrator`
in pytest's temp path.

Command:

```bash
ORCH_STATE_DIR=$(mktemp -d) ORCH_LOCAL_RUNTIME=$(mktemp -d) TMPDIR=$(mktemp -d) \
  /Users/teacher/.codex/py314/bin/python -m pytest -q \
  tests/test_synthesis_promotion_coverage.py \
  tests/test_issue1109_runtime_isolation.py \
  tests/test_issue1109_pr793_acceptance.py \
  -m 'not slow' -p no:cacheprovider --override-ini="addopts="
```

Result: `8 passed` in 0.39s. Counts: 8 passed, 0 failed.

## Clean local coverage sequence

Check date: 2026-11-05. Outcome 1 is two fresh JSON exports and a printed
missing-line/branch comparison for the exact parent and merge below. Run this
from the PR checkout with `bash`; stop after two failed attempts at a step or
60 minutes. This is a partial local measurement, never authoritative Gate evidence.

The previously selected interpreter `/Users/teacher/.codex/py314/bin/python` is
available. If unavailable, stop and report it; do not substitute silently.
Both snapshots use identical source scope and configuration, checked with `cmp`,
and the same baseline workload; only after adds the three historical nodes.
Every invocation allocates a fresh root; each side has its own coverage-data and
runtime directories exported before startup. Explicit combine reads only that
side's data directory. No existing `.coverage.*` files can enter the combine.
Branch instrumentation is enabled on both sides to calculate branch differences,
separate from all branch-disabled historical CI numbers above.

```bash
set -eu
PY=/Users/teacher/.codex/py314/bin/python
REPO=$(git rev-parse --show-toplevel)
RUN=$(mktemp -d /tmp/issue1109-coverage.XXXXXX)
export PY REPO RUN
"$PY" -c 'import sys, coverage; print("interpreter", sys.executable, sys.version); print("coverage", coverage.__version__)'
printf 'continuation_head=%s\nRUN=%s\n' "$(git rev-parse HEAD)" "$RUN"
for side in before after; do
  if [ "$side" = before ]; then
    revision=2f12281b795d749498e5641edcf32ef41420f499
  else
    revision=574c1694b2990e50ce0668589472e180f481019c
  fi
  mkdir -p "$RUN/$side/tree" "$RUN/$side/data" "$RUN/$side/state" "$RUN/$side/runtime" "$RUN/$side/tmp"
  git -C "$REPO" archive "$revision" | tar -x -C "$RUN/$side/tree"
  printf '%s revision=%s\n' "$side" "$revision"
done
cmp "$RUN/before/tree/pyproject.toml" "$RUN/after/tree/pyproject.toml"
cmp "$RUN/before/tree/src/synthesis_promotion.py" "$RUN/after/tree/src/synthesis_promotion.py"
for side in before after; do
  (
    cd "$RUN/$side/tree"
    export COVERAGE_FILE="$RUN/$side/data/.coverage"
    export ORCH_STATE_DIR="$RUN/$side/state" ORCH_LOCAL_RUNTIME="$RUN/$side/runtime" TMPDIR="$RUN/$side/tmp"
    export PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1
    unset ORCH_FEEDBACK_DB ORCH_CAPABILITIES_PATH ORCH_FEATURES_PATH COVERAGE_PROCESS_START
    set -- tests/test_synthesis_promotion.py tests/test_synthesis_delivery_discovery.py
    if [ "$side" = after ]; then
      set -- "$@" tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning
    fi
    printf '%s workload: %s\n' "$side" "$*"
    "$PY" -m coverage run --branch --source=src --rcfile=pyproject.toml -m pytest "$@" -m 'not slow' -q -p no:cacheprovider --override-ini='addopts='
    "$PY" -m coverage combine --rcfile=pyproject.toml "$RUN/$side/data"
    "$PY" -m coverage report --rcfile=pyproject.toml --include='*/synthesis_promotion.py'
    "$PY" -m coverage json --rcfile=pyproject.toml --include='*/synthesis_promotion.py' -o "$RUN/$side/coverage.json"
  )
done
"$PY" - <<'PY'
import json
import os
from pathlib import Path
root = Path(os.environ['RUN'])
files = []
for side in ('before', 'after'):
    payload = json.loads((root / side / 'coverage.json').read_text())
    assert payload['meta']['branch_coverage'] is True
    data = payload['files']['src/synthesis_promotion.py']
    print(side, json.dumps(data['summary'], sort_keys=True))
    files.append(data)
before, after = files
for field in ('missing_lines', 'missing_branches'):
    def units(data):
        return {tuple(v) if isinstance(v, list) else v for v in data[field]}
    old, new = units(before), units(after)
    print(field, 'before', len(old), 'after', len(new), 'reduction', len(old) - len(new))
    print('newly covered', sorted(old - new))
    print('newly missing', sorted(new - old))
PY
```

## Continuation execution record

Checked 2026-11-05 on head `26a27a785311c43ba7f47e99f4c1fd9445518539`
with the scoped uncommitted test/document changes in this continuation. Interpreter:
`/Users/teacher/.codex/py314/bin/python`, Python 3.14.3; pytest 9.1.1; coverage 7.17.0.
Historical interpreter availability statements above describe their original hosts.

The clean sequence above was executed without modification using `bash`. Actual
root: `/tmp/issue1109-coverage.W0BNmV`. Both JSON exports exist at
`before/coverage.json` and `after/coverage.json` under that root. `cmp` passed for
configuration and production source. The archive snapshots are not git checkouts,
so pytest also printed `tracked test inputs: NOT CHECKED`; the inputs came from
`git archive` of the two explicitly printed revisions.

Actual coverage output (progress dots and duplicate TOTAL/JSON summary lines omitted):

```text
interpreter /Users/teacher/.codex/py314/bin/python 3.14.3 (main, Feb 12 2026, 00:29:58) [Clang 21.1.4 ]
coverage 7.17.0
continuation_head=26a27a785311c43ba7f47e99f4c1fd9445518539
RUN=/tmp/issue1109-coverage.W0BNmV
before revision=2f12281b795d749498e5641edcf32ef41420f499
after revision=574c1694b2990e50ce0668589472e180f481019c
79 passed in 14.01s
Combined 1 file
src/synthesis_promotion.py     984    490    328     58  48.5%
Wrote JSON report to /tmp/issue1109-coverage.W0BNmV/before/coverage.json
82 passed in 14.66s
Combined 1 file
src/synthesis_promotion.py     984    461    328     54  51.8%
Wrote JSON report to /tmp/issue1109-coverage.W0BNmV/after/coverage.json
missing_lines before 490 after 461 reduction 29
newly covered [468, 469, 470, 471, 472, 473, 474, 475, 476, 477, 478, 479, 480, 481, 482, 484, 485, 486, 487, 510, 511, 517, 527, 533, 623, 628, 673, 674, 675]
newly missing []
missing_branches before 186 after 172 reduction 14
newly covered [(472, 473), (478, 479), (478, 480), (482, 484), (485, 486), (485, 487), (509, 510), (516, 517), (526, 527), (532, 533), (622, 623), (672, 673), (673, 674), (673, 682)]
newly missing []
```

These partial-suite results reproduce 29 fewer missing lines and 14 fewer missing
branches. They do not replace the historical CI reduction of 26 statements or
change the 154-unit shortfall.

Focused acceptance and separate restored-original-node verification used this
exact shell sequence, with runtime roots exported before either interpreter starts:

```bash
set -eu
PY=/Users/teacher/.codex/py314/bin/python
CHECK=$(mktemp -d /tmp/issue1109-focused.XXXXXX)
mkdir -p "$CHECK/state" "$CHECK/runtime" "$CHECK/tmp"
export ORCH_STATE_DIR="$CHECK/state" ORCH_LOCAL_RUNTIME="$CHECK/runtime" TMPDIR="$CHECK/tmp"
unset ORCH_FEEDBACK_DB ORCH_CAPABILITIES_PATH ORCH_FEATURES_PATH
printf 'head=%s\nCHECK=%s\n' "$(git rev-parse HEAD)" "$CHECK"
"$PY" -m pytest -q tests/test_synthesis_promotion_coverage.py tests/test_issue1109_runtime_isolation.py tests/test_issue1109_pr793_acceptance.py -m 'not slow' -p no:cacheprovider --override-ini='addopts='
"$PY" -m pytest -q \
 tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head \
 tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command \
 tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning \
 -m 'not slow' -p no:cacheprovider --override-ini='addopts='
git diff -- src/synthesis_promotion.py
```

Actual `CHECK=/tmp/issue1109-focused.ORKVUV`, head as above. Printed results:
`8 passed in 0.46s`, then `3 passed in 0.10s`. The final production diff printed
nothing: `git diff -- src/synthesis_promotion.py` is empty. No production mutations
were repeated in this continuation.

Review snapshot: GitHub returned nine total threads, seven unresolved and two
already resolved, rather than eight active. All nine were re-read. The seven
remaining findings cover Gate artifact absence, complete reporting, the five-test
selector, fresh parallel-data paths, the after export, four-test provenance, and
collection-time production imports. Their dispositions are the scoped changes
above; the runtime-isolation module received the same path correction. Required
CI, current-head review, the seven-minute floor, and `merge_guard` remain required.

After merging main `1ebdb4fba93abe66c38c0d8702076255401215b6` to resolve the
single `config/test-shards.json` registration conflict, the same focused shell
sequence above was run again on head
`423e30ac2cf5c786084a330292a34d4a9ecbd6bf`, Python 3.14.3, with fresh
`CHECK=/tmp/issue1109-focused.E18uCs`. It printed `8 passed in 0.48s`, then
`3 passed in 0.07s`; the production diff again printed nothing. All issue #1109
and main's issue #1150 registrations were retained; JSON parsing and uniqueness
checks passed. No coverage sequence or deliberate mutations were repeated.
