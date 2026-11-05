# Issue #1109: PR #793 synthesis-promotion acceptance evidence

Recorded 2026-11-05 on checkout `612dc68374be9eefcc03db3f909e59eaba020644`. No new CI run and no new Gate run were started. Historical runs are named below and are not this checkout's checks.

`docs/AGENT_ISSUE_FORMAT.md` is the issue shape used to read #791 and #1109. The facts below come from `gh pr view 793`, `gh issue view 791`, `git log -1 --format='%H%n%P' 574c1694b2990e50ce0668589472e180f481019c`, and the downloaded `coverage.json` artifacts.

## Identification

| Fact | Value |
| --- | --- |
| PR | [#793](https://github.com/stranske/Orchestrator/pull/793), merged 2026-10-19T14:37:36Z |
| Merge revision | `574c1694b2990e50ce0668589472e180f481019c` (squash, one parent) |
| Parent revision | `2f12281b795d749498e5641edcf32ef41420f499` |
| PR head | `891a157836ac6556bbecd4dc192680cb5b9c3933` (GitHub API; that object is not in this checkout) |
| PR base at open | `a1a86b99381def8d8774c6b469008d4215223103` |
| Diff parent..merge | `tests/test_synthesis_promotion_coverage.py` only (131 lines). No production diff |
| Production module | `src/synthesis_promotion.py`, blob `d763725850a098a07fd2a425446bea1063e1b575` at the parent, the merge, and this checkout |
| Completion probe | `default_completion_probe` |
| Gate runner | `_run_gate` |
| Verification | `verify_synthesis` |

The three tests added by PR #793:

- `tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head`
- `tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command`
- `tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning`

Original coverage contract, from issue #791: cover the next ranked targets until about 180 uncovered statements+branches are covered. The stated order was `src/synthesis_promotion.py` (168 uncovered statements+branches, 3 escaped-defect signals, churn 6) then `src/codemod_lane.py` (123, 3 escaped-defect signals, churn 6). The authority named in that issue is the whole CI run, `python3 src/verify.py --coverage`, not one test file's `pytest --cov`. A partial PR meets the criterion when the body states the shortfall. Production changes were allowed only where a test found a bug. PR #793 changed no production code and did not touch `src/codemod_lane.py`.

## CI pytest and coverage jobs

Commands are from `.github/workflows/ci.yml` and `.github/workflows/pr-00-gate.yml` at `574c169`. Results are the historical runs. This document does not record a new passing CI run.

### Passing CI pytest (historical)

Revision `891a157836ac6556bbecd4dc192680cb5b9c3933` (PR head, not the merge commit).

- Workflow: CI, run [39260265878](https://github.com/stranske/Orchestrator/actions/runs/39260265878), event `pull_request`, conclusion success
- Job: `verify.py (real pytest counts + selftests + capability gates)`, id `118085764899`, conclusion success
- Install: `python -m pip install --quiet pytest pyyaml coverage` on Python 3.14
- Command that runs for `pull_request`: `python3 src/verify.py --floor-may-lag`
- Environment: `ORCH_STATE_DIR=${{ runner.temp }}/orch-state`, `ORCH_LOCAL_RUNTIME=${{ runner.temp }}/orch-runtime`
- Printed result: `5791 passed, 0 failed, 27/31 max skipped (5818 collected; floor 5815 — 3 BEHIND)`
- The floor gap was reported and the job still succeeded because of `--floor-may-lag`

### Merge-revision CI pytest did not pass (historical)

Revision `574c1694b2990e50ce0668589472e180f481019c`.

- Workflow: CI, run [39270663547](https://github.com/stranske/Orchestrator/actions/runs/39270663547), event `push`, conclusion failure
- Job: `verify.py (real pytest counts + selftests + capability gates)`, id `118121764897`, conclusion failure
- Command for a push: `python3 src/verify.py` (no `--floor-may-lag`)
- Printed pytest counts were the same `5791 passed, 0 failed` and the same floor gap. The job failed on `floor is BEHIND reality: 5818 collected > floor 5815`, not on a failing test
- Combined-coverage job on this same run succeeded (id `118121764711`)

The parent revision `2f12281b795d749498e5641edcf32ef41420f499` has no CI workflow run.

### Coverage configuration and the Gate command

At the merge revision, `[tool.coverage.run]` in `pyproject.toml` sets `source = ["src"]`, `branch = false`, `parallel = true`, and `relative_files = true`.

CI's combined-coverage job is the measurement the repository calls the honest number. On every event it runs:

```bash
python3 src/verify.py --coverage --floor-may-lag
```

with the same two runtime variables, then publishes `coverage.json` as artifact `gate-coverage` and `coverage-trend.json` as artifact `gate-coverage-trend`. The verify job does not publish that file. `combined-coverage` (the raw `.coverage` plus the log) had `retention-days: 7` and is no longer listed.

The Gate workflow's own coverage steps, at this revision, download `gate-*` artifacts into `gate_artifacts/downloads` and run `computeCoverageStats` over `coverage/runtimes`, writing `gate-coverage.json` in the job. Those Gate-run files were not uploaded.

| Measurement | Run | Revision | `gate-coverage` artifact | What it is |
| --- | --- | --- | --- | --- |
| Pre-addition combined coverage | [39254310194](https://github.com/stranske/Orchestrator/actions/runs/39254310194), job `118065628727` success | `08c7b70a8f38af8217bdf1dd8d8ce9adad6ac4af` | id `12187861769`, downloaded, not expired | Historical CI artifact. The CI run's verify job failed; this coverage job succeeded |
| PR-head combined coverage | [39260265878](https://github.com/stranske/Orchestrator/actions/runs/39260265878), job `118085764499` success | `891a157836ac6556bbecd4dc192680cb5b9c3933` | id `12191166790`, downloaded | Historical CI artifact |
| Merge combined coverage | [39270663547](https://github.com/stranske/Orchestrator/actions/runs/39270663547), job `118121764711` success | `574c1694b2990e50ce0668589472e180f481019c` | id `12194284584`, downloaded | Historical CI artifact |

`git diff --stat 08c7b70a8f38af8217bdf1dd8d8ce9adad6ac4af 2f12281b795d749498e5641edcf32ef41420f499` is only `.verify-floor.json`. `src/synthesis_promotion.py` and `src/router.py` have the same blobs on `08c7b70`, the parent, and the merge. The pre-addition artifact measures the parent revision's production source. It is not a CI run of the parent SHA.

Gate workflow runs retain nothing:

- PR Gate [39260266708](https://github.com/stranske/Orchestrator/actions/runs/39260266708), conclusion success, `artifacts.total_count = 0`
- Merge Gate [39270664385](https://github.com/stranske/Orchestrator/actions/runs/39270664385), conclusion success, `artifacts.total_count = 0`

`ci.yml` at this revision says Maint Coverage Guard reads `pr-00-gate.yml` and cannot see the combined-coverage job. The per-file numbers below are the CI artifacts that happen to be named `gate-coverage`. They are not artifacts of the Gate workflow. Gate's `gate-coverage.json` / coverage-summary files are unavailable.

## Before and after coverage

Same configuration on every artifact: coverage.py 7.16.2, `branch_coverage: false`, `source = ["src"]`, command `python3 src/verify.py --coverage --floor-may-lag`. Scope is `src/synthesis_promotion.py`. `src/codemod_lane.py` is included because it was the second ranked target and was not changed.

`missing_branches` and `num_branches` are absent from every file summary. Uncovered branches were not collected. They are not reported as zero.

Job log lines match the JSON:

- Before, run 39254310194: `src/synthesis_promotion.py  984  168  82.9%`
- PR head, run 39260265878: `src/synthesis_promotion.py  984  142  85.6%`
- Merge, run 39270663547: `src/synthesis_promotion.py  984  142  85.6%`

| | Uncovered statements | Uncovered branches |
| --- | ---: | --- |
| Before, run 39254310194, `src/synthesis_promotion.py` | 168 of 984 (816 covered) | not collected |
| After, PR-head run 39260265878 | 142 of 984 (842 covered) | not collected |
| After, merge run 39270663547 | 142 of 984 (842 covered) | not collected |
| `src/codemod_lane.py` before and after | 123 of 601 (478 covered) | not collected |

The issue's "168 uncovered statements+branches" equals this statement miss count, and 123 equals `codemod_lane.py`. With `branch = false`, the contract unit that the artifact can support is the statement count. No branch term was in the sum.

Statements newly covered on `src/synthesis_promotion.py`, before artifact minus merge artifact: 26. Lines `468–482`, `484–487`, `527`, `533`, `623`, `628`, `673–675`. Line 483 was missing before and is still missing. No line became newly missing. `src/codemod_lane.py` did not change.

- Achieved reduction under the original contract: **26** statements. Branches: not measured, so they add nothing to the reduction.
- Objective: about 180. Shortfall: **154** (180 − 26).
- The first target still has 142 uncovered statements, so it was not fully covered. The second target was not started.

Whole-tree missing statements moved from 9981 to 9954. Two files account for that: `src/synthesis_promotion.py` (26) and `src/router.py` (55 to 54). `src/router.py` has the same blob on all three revisions, so that one statement is measurement drift, not this PR. The module reduction attributable to the three tests is 26.

### Local reproduction (not a CI or Gate artifact)

Coverage.py 7.17.2, same `pyproject.toml` (`source = ["src"]`, `branch = false`), on detached worktrees. This is not the whole suite, so the miss counts are not 168 and 142.

Parent worktree `2f12281b795d749498e5641edcf32ef41420f499`, selftest only:

```bash
ORCH_STATE_DIR=/tmp/orch1109-cov-before/state ORCH_LOCAL_RUNTIME=/tmp/orch1109-cov-before/runtime \
  PYTHONPATH=src /tmp/orch1109-venv/bin/python -m coverage run --rcfile=pyproject.toml --source=src \
  -m synthesis_promotion --selftest
```

Result: 466 covered, 518 missing, branches not collected. All 26 lines the CI artifacts show as newly covered are in this missing set.

Merge worktree `574c1694b2990e50ce0668589472e180f481019c`, the three tests only:

```bash
ORCH_STATE_DIR=/tmp/orch1109-cov-after/state ORCH_LOCAL_RUNTIME=/tmp/orch1109-cov-after/runtime \
  PYTHONPATH=src /tmp/orch1109-venv/bin/python -m coverage run --rcfile=pyproject.toml --source=src \
  -m pytest tests/test_synthesis_promotion_coverage.py -q -m "not slow"
```

Result: `3 passed`, 181 covered, 803 missing, 18.394%. That is the 181/984 figure PR #793 reported and labeled as not comparable to CI. Those 181 executed lines include all 26 CI-delta lines.

The three tests execute 77 statements the selftest misses. Of those, 51 were already covered in the before CI artifact by the rest of the suite, and 26 are the CI delta. None of the 77 remain missing in the merge CI artifact. A local selftest-plus-tests combine on the merge worktree reported 543 covered and 441 missing. That 77-statement local drop is the wrong denominator for the contract; the CI whole-run drop is 26.

## Runtime initialization

`import synthesis_promotion` imports `capabilities`, `feedback`, `local_verify`, and `runtime_ac` at `src/synthesis_promotion.py`. These bindings happen at that import, before any test body:

- `feedback.LOCAL_RUNTIME` and `capabilities.LOCAL_RUNTIME` read `ORCH_LOCAL_RUNTIME`, otherwise `~/.codex/orchestrator`
- `feedback.DB_PATH` is then `resolve_db_path()`, which prefers `ORCH_FEEDBACK_DB`, then `ORCH_LOCAL_RUNTIME/feedback/orchestrator.db`, then `ORCH_STATE_DIR/feedback/orchestrator.db`
- `capabilities.REG` reads `ORCH_CAPABILITIES_PATH`, otherwise `LOCAL_RUNTIME/capabilities.json`. It does not read `ORCH_STATE_DIR`
- `capabilities.FEATURES_REG` calls `paths.runtime_state_path`, which reads `ORCH_LOCAL_RUNTIME` at import

`tests/test_synthesis_promotion_coverage.py` sets both variables in an autouse fixture after `import synthesis_promotion`, so that fixture does not move the cached bindings. The three tests exercise `default_completion_probe`, `_run_gate`, and `verify_synthesis` without reading those paths.

`tests/test_issue1109_runtime_isolation.py` starts a new interpreter with both variables set to pytest temporary directories before the process starts, then imports `synthesis_promotion`. Checked 2026-11-05 with the command in the next section (`5 passed`, which includes these two tests):

- Both temporary paths are present in the child before the import
- `feedback.LOCAL_RUNTIME`, `capabilities.LOCAL_RUNTIME`, `capabilities.REG`, and `capabilities.FEATURES_REG` resolve under the runtime directory
- `feedback.DB_PATH` is `<runtime>/feedback/orchestrator.db`
- None of those selected paths sit under `~/.codex/orchestrator`
- Assigning new environment values after the import leaves the bound objects in place

A second child sets only `ORCH_STATE_DIR` and unsets `ORCH_LOCAL_RUNTIME`. `feedback.DB_PATH` is then `<state>/feedback/orchestrator.db`. `capabilities.REG` still resolves to `~/.codex/orchestrator/capabilities.json`. Both variables have to be set before import for the capability ledger path to leave the shared runtime.

## Baseline and deliberate breaks

No repository-selected interpreter file is present: `~/.codex/python-bin` is absent and `/opt/anaconda3/bin/python3` is absent. The interpreter on `PATH` is `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3` (Python 3.14.10). Pytest is not installed on that interpreter, so the commands use a virtualenv created from it: `/tmp/orch1109-venv/bin/python` (`readlink` resolves to `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3.14`).

Checkout: `612dc68374be9eefcc03db3f909e59eaba020644`. Production blob matches the merge revision. Temporary paths: `ORCH_STATE_DIR=/tmp/orch1109-pytest/state`, `ORCH_LOCAL_RUNTIME=/tmp/orch1109-pytest/runtime`.

`python -m pytest --collect-only -q` discovered these node IDs:

- `tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head`
- `tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command`
- `tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning`

Baseline before the mutations, and again after every mutation was restored (`3 passed in 0.21s`, then `5 passed in 0.52s` with the two isolation tests):

```bash
ORCH_STATE_DIR=/tmp/orch1109-pytest/state ORCH_LOCAL_RUNTIME=/tmp/orch1109-pytest/runtime \
  /tmp/orch1109-venv/bin/python -m pytest -q \
  tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head \
  tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command \
  tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning \
  -m "not slow"
```

Each break was one substitution in `src/synthesis_promotion.py` (each needle occurred once), then that node only, then the file was written back to its original text. `git diff -- src/synthesis_promotion.py` was empty afterward. PR #793's body records only the completion-probe break and does not name a revision, a command, and a result for the other two tests, so all three were run here.

| Test | Mutation | Named failure | After restore |
| --- | --- | --- | --- |
| `test_completion_probe_requires_a_success_marker_and_new_head` | `default_completion_probe`: `if rc != 0` changed to `if rc == 0` | `AssertionError`: status `interrupted`, reason `synthesis marker rc=0`, instead of status `complete` | `1 passed in 0.15s` |
| `test_run_gate_never_runs_non_allowlisted_command` | `_run_gate`: `if not _safe_command(argv)` changed to `if False and not _safe_command(argv)` | `AssertionError: subprocess must not run an unallowlisted gate` | `1 passed in 0.15s` |
| `test_verify_synthesis_records_unallowlisted_local_command_without_spawning` | `verify_synthesis`: `if not _safe_command(local_argv)` changed to `if False and not _safe_command(local_argv)` | `AssertionError: rejected command must not reach local verification` | `1 passed in 0.15s` |

## Ranking rationale

Issue #791 states the selection rule: escaped defects, then churn, then uncovered mass, skipping files other lanes had reserved, bundling targets until about 180 units are covered. Its stated pair was `src/synthesis_promotion.py` then `src/codemod_lane.py`.

Reproduced 2026-11-05 from the parent worktree against the pre-addition `coverage.json` (tier 1 source: git, because the feedback store was not readable):

```bash
ORCH_STATE_DIR=/tmp/orch1109-rank/state ORCH_LOCAL_RUNTIME=/tmp/orch1109-rank/runtime \
  PYTHONPATH=src /tmp/orch1109-venv/bin/python -m escaped_defect_priority \
  --repo /tmp/orch1109-wt-before \
  --coverage-json /tmp/orch1109/before/gate-coverage/coverage.json \
  --limit 400 --json
```

`uncovered_by_file` on that artifact is 168 for `src/synthesis_promotion.py` and 123 for `src/codemod_lane.py`, matching the issue and the table above. The same command's defect and churn figures do not match the issue text. With a 180-day lookback ending 2026-11-05, `synthesis_promotion.py` ranked 56th (escaped 3.0, churn 11, uncovered 168) and `codemod_lane.py` ranked 41st (escaped 5.0, churn 9, uncovered 123). The top of that list is `src/dispatcher.py`, `src/feedback.py`, and `src/capability_advisor.py`. The issue's reserved-file list excluded several of those from the round. The uncovered-statement counts are the part of the original ranking this artifact reproduces. The October dispatch order is the contract's stated order; this later rank does not select those two files first.

## Historical gaps

- No new CI pytest run and no new Gate run were executed for this evidence.
- The passing pytest job is the PR-head run. The merge revision's pytest job failed the floor equality after the same `5791 passed, 0 failed`.
- The parent SHA has no CI run. The before coverage artifact is the previous main push, which differs only by `.verify-floor.json`.
- Gate workflow runs for the PR head and the merge retain zero artifacts. Gate's `computeCoverageStats` output and coverage summary are unavailable. The numbers above are the CI combined artifacts published under the name `gate-coverage`.
- Branch coverage was disabled, so uncovered branches and any branch-only reduction are unmeasured. The 154-unit shortfall is against statement coverage only.
- Local coverage used coverage.py 7.17.2 and a subset of the suite. It explains which of the 26 lines the three tests execute. It is not a substitute for the 7.16.2 combined artifacts.
- One statement in `src/router.py` differs between the before and merge artifacts even though that file's blob does not. It is not counted in the 26.
