# Issue #1109: PR #793 synthesis-promotion acceptance evidence

Recorded 2026-11-05. This document is historical evidence plus one local reproduction. No new CI or Gate run was started.

Provenance: `closer-core/v1 followup-of=stranske/Orchestrator#791@793`.

## Identification

Read `docs/AGENT_ISSUE_FORMAT.md`. Inspected with `gh pr view 793`, `gh issue view 791`, and `git log -1 --format='%H%n%P' 574c1694b2990e50ce0668589472e180f481019c`.

| Fact | Value |
| --- | --- |
| PR | [#793](https://github.com/stranske/Orchestrator/pull/793), merged 2026-10-19T14:37:36Z |
| Merge revision | `574c1694b2990e50ce0668589472e180f481019c` (squash, one parent) |
| Parent revision | `2f12281b795d749498e5641edcf32ef41420f499` (`chore(floor): reconcile the collection floor on main`, 2026-10-19T12:51:36Z) |
| PR head | `891a157836ac6556bbecd4dc192680cb5b9c3933` (from the GitHub API; that object is not in this checkout) |
| Added file | `tests/test_synthesis_promotion_coverage.py` (131 lines at merge; no production diff) |
| Production module | `src/synthesis_promotion.py` blob `d763725850a098a07fd2a425446bea1063e1b575` at the parent, at the merge, and at this checkout's `HEAD` |
| Completion probe | `default_completion_probe` |
| Gate runner | `_run_gate` |
| Verification | `verify_synthesis` |

The three tests added by PR #793:

- `tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head`
- `tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command`
- `tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning`

Original coverage contract, from issue #791: one PR covers the next ranked targets until about 180 uncovered statements+branches are covered. The stated order was `src/synthesis_promotion.py` (168 uncovered, 3 escaped-defect signals, churn 6) then `src/codemod_lane.py` (123 uncovered, 3 escaped-defect signals, churn 6). The authority named in that issue is the repository's whole CI run (`python3 src/verify.py --coverage`), not a single-file `pytest --cov`. A partial PR meets the criterion when the body states the shortfall. Production changes were allowed only for a bug a test found. PR #793 changed no production code.

## CI pytest and Gate coverage

Commands below are from `.github/workflows/ci.yml` and `.github/workflows/pr-00-gate.yml` at the merge revision. Results are from the historical runs. They are not a new run.

### Passing CI pytest (historical)

Revision `891a157836ac6556bbecd4dc192680cb5b9c3933` (PR head, not the merge commit).

- Workflow: CI, run [39260265878](https://github.com/stranske/Orchestrator/actions/runs/39260265878), event `pull_request`, conclusion success
- Job: `verify.py (real pytest counts + selftests + capability gates)`, id `118085764899`, conclusion success
- Install: `python -m pip install --quiet pytest pyyaml coverage` on Python 3.14
- Command, because the event was `pull_request`: `python3 src/verify.py --floor-may-lag`
- Environment: `ORCH_STATE_DIR=${{ runner.temp }}/orch-state`, `ORCH_LOCAL_RUNTIME=${{ runner.temp }}/orch-runtime`
- Printed result: `5791 passed, 0 failed, 27/31 max skipped (5818 collected; floor 5815 — 3 BEHIND)`
- `--floor-may-lag` reported that floor gap and the job still succeeded

### Merge-revision CI pytest did not pass (historical)

Revision `574c1694b2990e50ce0668589472e180f481019c`.

- Workflow: CI, run [39270663547](https://github.com/stranske/Orchestrator/actions/runs/39270663547), event `push`, conclusion failure
- Job: `verify.py (real pytest counts + selftests + capability gates)`, id `118121764897`, conclusion failure
- Command, because the event was `push`: `python3 src/verify.py` (no `--floor-may-lag`)
- Printed pytest counts were the same `5791 passed, 0 failed` and the same floor gap. The job failed on `floor is BEHIND reality: 5818 collected > floor 5815`, not on a failing test
- Combined-coverage job on this same run succeeded (id `118121764711`)

The parent revision `2f12281b795d749498e5641edcf32ef41420f499` has no CI workflow run (`gh run list --commit 2f12281b795d749498e5641edcf32ef41420f499 --workflow CI` is empty).

### Coverage jobs and artifacts

At the merge revision, CI's combined-coverage job is the measurement the repository calls the honest number:

```bash
python3 src/verify.py --coverage --floor-may-lag
```

with the same two runtime variables. It publishes `coverage.json` as artifact `gate-coverage` and `coverage-trend.json` as artifact `gate-coverage-trend`. `pyproject.toml` at both revisions sets `[tool.coverage.run] source = ["src"]` and `branch = false`.

| Measurement | Run | Revision | Artifact `gate-coverage` | Status |
| --- | --- | --- | --- | --- |
| Pre-addition combined coverage | [39254310194](https://github.com/stranske/Orchestrator/actions/runs/39254310194) job success for combined coverage | `08c7b70a8f38af8217bdf1dd8d8ce9adad6ac4af` | id `12187861769`, not expired | Historical CI artifact |
| PR-head combined coverage | [39260265878](https://github.com/stranske/Orchestrator/actions/runs/39260265878) job `118085764499` success | `891a157836ac6556bbecd4dc192680cb5b9c3933` | id `12191166790`, not expired | Historical CI artifact |
| Merge combined coverage | [39270663547](https://github.com/stranske/Orchestrator/actions/runs/39270663547) job `118121764711` success | `574c1694b2990e50ce0668589472e180f481019c` | id `12194284584`, not expired | Historical CI artifact |

`git diff --stat 08c7b70a8f38af8217bdf1dd8d8ce9adad6ac4af 2f12281b795d749498e5641edcf32ef41420f499` is only `.verify-floor.json`. The production blob of `src/synthesis_promotion.py` is identical, so the pre-addition artifact measures the parent revision's production source. It is not a CI run of the parent SHA itself.

Gate workflow runs retain nothing:

- PR Gate [39260266708](https://github.com/stranske/Orchestrator/actions/runs/39260266708), conclusion success, `artifacts.total_count = 0`
- Merge Gate [39270664385](https://github.com/stranske/Orchestrator/actions/runs/39270664385), conclusion success, `artifacts.total_count = 0`

At this revision, Gate's `Compute coverage stats` step reads downloaded `gate-*` python-ci runtime coverage (`coverage/runtimes`) and writes `gate-coverage.json`. Those Gate-run files are not available. CI's comment at the time says Maint Coverage Guard reads `pr-00-gate.yml` and cannot see the combined-coverage job. The per-file numbers below are the CI combined artifacts, which use the artifact name `gate-coverage`. They are not artifacts of the Gate workflow run.

## Before and after coverage

Same configuration at both measured revisions: coverage.py 7.16.2, `branch_coverage: false`, `source = ["src"]`, combined `verify.py --coverage`. Scope reported here is `src/synthesis_promotion.py` and the untouched second target `src/codemod_lane.py`.

`missing_branches` and `num_branches` are JSON null in every artifact. Uncovered branches were not collected. They are not reported as zero.

| | Uncovered statements | Uncovered branches |
| --- | ---: | --- |
| Before, run 39254310194, `src/synthesis_promotion.py` | 168 of 984 (816 covered, 82.9%) | not collected |
| After, merge run 39270663547, same file | 142 of 984 (842 covered, 85.6%) | not collected |
| After, PR-head run 39260265878, same file | 142 of 984 | not collected |
| `src/codemod_lane.py` before and after | 123 of 601 | not collected |

The issue's "168 uncovered statements+branches" equals this statement miss count exactly, and 123 equals `codemod_lane.py`. With `branch = false`, that contract unit is the statement count. No separate branch term was in the sum.

- Statements newly covered: 26. Lines: 468–487, 527, 533, 623, 628, 673–675. No line became newly missing.
- Branches newly covered: not measured.
- Combined reduction under the original contract: 26.
- Objective: about 180. Shortfall: 154 (180 − 26).
- Whole-tree missing statements moved from 9981 to 9954 on the merge artifact (totals percent 86.76% to 86.80%). That tree delta is larger than 26 because other files changed between `08c7b70` and the merge; the module delta is 26.

Target limitations: `synthesis_promotion.py` still has 142 uncovered statements, so the first target was not fully covered. `codemod_lane.py` was not changed (123 still uncovered). Branch coverage is off, so a branch-only gap cannot move this measurement. PR #793's body left the after value to Gate and reported a non-comparable local `181/984` (18.4%) from the new file alone.

### Local reproduction (not the Gate or CI artifact)

Coverage.py 7.17.2, same `pyproject.toml` (`source = ["src"]`, `branch = false`), on worktrees of the parent and merge revisions. This omits the rest of the suite, so the miss counts are not the 168/142 contract numbers.

Selftest only, both revisions (`python -m coverage run --rcfile=pyproject.toml --source=src -m synthesis_promotion --selftest`): 466 covered, 518 missing, branches null. Reduction 0, which matches the identical production blob.

Three PR #793 tests only, merge worktree:

```bash
PYTHONPATH=src python -m coverage run --rcfile=pyproject.toml --source=src \
  -m pytest tests/test_synthesis_promotion_coverage.py -q -m "not slow"
```

Result: 181 covered, 803 missing, 18.394% — the same 181/984 figure PR #793 reported. All 26 lines that the CI artifacts show as newly covered are in this run's `executed_lines`, and all 26 are missing from the selftest run.

## Runtime initialization

`import synthesis_promotion` imports `capabilities`, `feedback`, `local_verify`, and `runtime_ac` (`src/synthesis_promotion.py` import block). These bindings happen at that import, before any test body:

- `feedback.LOCAL_RUNTIME` from `ORCH_LOCAL_RUNTIME`, else `~/.codex/orchestrator` (`src/feedback.py` assignment before `resolve_db_path`)
- `feedback.DB_PATH` via `resolve_db_path()`, which prefers `ORCH_FEEDBACK_DB`, then `ORCH_LOCAL_RUNTIME`, then `ORCH_STATE_DIR`
- `capabilities.LOCAL_RUNTIME` and `capabilities.REG` from `ORCH_LOCAL_RUNTIME` / `ORCH_CAPABILITIES_PATH`
- `capabilities.FEATURES_REG` from `paths.runtime_state_path`, which reads `ORCH_LOCAL_RUNTIME` when called during that import

`tests/test_synthesis_promotion_coverage.py` imports `synthesis_promotion` at module level. The autouse fixture `isolated_orchestrator_paths` (added later by #1061 in `2a6cfbc`) calls `monkeypatch.setenv` only after that import, so it does not move the cached bindings. The tests in this file exercise `default_completion_probe`, `_run_gate`, and `verify_synthesis` without reading those cached paths.

`tests/test_issue1109_runtime_isolation.py` starts a new interpreter with both variables set to pytest temporary directories before the process starts, then imports `synthesis_promotion`. Checked 2026-11-05: the child records both temporary paths before the import; `feedback.LOCAL_RUNTIME`, `capabilities.LOCAL_RUNTIME`, `capabilities.REG`, and `capabilities.FEATURES_REG` are under the runtime directory; `feedback.DB_PATH` is `<runtime>/feedback/orchestrator.db`. None of those selected paths sit under `~/.codex/orchestrator`. Assigning new environment values after the import leaves the bound objects in place.

A second child sets only `ORCH_STATE_DIR` and unsets `ORCH_LOCAL_RUNTIME`. `feedback.DB_PATH` is then `<state>/feedback/orchestrator.db`. `capabilities.REG` still resolves to the shared default, because that binding does not read `ORCH_STATE_DIR`. Both variables have to be set before import for the capability ledger path to leave the shared runtime.

## Baseline and deliberate breaks

No repository-selected interpreter is present: `~/.codex/python-bin` is absent and `/opt/anaconda3/bin/python3` is absent. The interpreter used is `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3` (Python 3.14.10). Pytest is not installed on that interpreter, so the commands use a temporary virtualenv whose `python` is that same binary: `/tmp/orch1109-venv/bin/python`.

Checkout under test: `d713f8fb3df91c134084d2b3c13c21c42772e387`. Production blob matches the merge revision. Temporary paths: `ORCH_STATE_DIR=/tmp/orch1109-pytest/state`, `ORCH_LOCAL_RUNTIME=/tmp/orch1109-pytest/runtime`.

After every mutation was restored, this command passed (`3 passed in 0.07s`, exit 0):

```bash
ORCH_STATE_DIR=/tmp/orch1109-pytest/state ORCH_LOCAL_RUNTIME=/tmp/orch1109-pytest/runtime \
  /tmp/orch1109-venv/bin/python -m pytest -q \
  tests/test_synthesis_promotion_coverage.py::test_completion_probe_requires_a_success_marker_and_new_head \
  tests/test_synthesis_promotion_coverage.py::test_run_gate_never_runs_non_allowlisted_command \
  tests/test_synthesis_promotion_coverage.py::test_verify_synthesis_records_unallowlisted_local_command_without_spawning \
  -m "not slow"
```

Each break was one unique substitution in `src/synthesis_promotion.py`, then that node only, then `git checkout -- src/synthesis_promotion.py`. `git diff -- src/synthesis_promotion.py` was empty afterward. PR #793's recorded break covered only the completion probe and did not name a revision-local command that can be reused for the other two tests, so all three were run here.

| Test | Mutation | Named failure | After restore |
| --- | --- | --- | --- |
| `test_completion_probe_requires_a_success_marker_and_new_head` | `default_completion_probe`: `if rc != 0` changed to `if rc == 0` | `AssertionError`: status `interrupted` / `synthesis marker rc=0` instead of `complete` | 1 passed |
| `test_run_gate_never_runs_non_allowlisted_command` | `_run_gate`: `if not _safe_command(argv)` changed to `if False and not _safe_command(argv)` | `AssertionError: subprocess must not run an unallowlisted gate` | 1 passed |
| `test_verify_synthesis_records_unallowlisted_local_command_without_spawning` | `verify_synthesis`: `if not _safe_command(local_argv)` changed to `if False and not _safe_command(local_argv)` | `AssertionError: rejected command must not reach local verification` | 1 passed |

## Ranking rationale

Issue #791 states the selection rule: escaped defects, then churn, then uncovered mass, skipping files other lanes had reserved, and bundling targets until about 180 units are covered. Its stated pair was `src/synthesis_promotion.py` then `src/codemod_lane.py`.

Reproduced 2026-11-05 from the parent worktree against the pre-addition `coverage.json` (tier 1 source: git, feedback store not readable):

```bash
PYTHONPATH=src python -m escaped_defect_priority \
  --repo /tmp/orch1109-wt-before \
  --coverage-json /tmp/orch1109/before/coverage.json \
  --limit 400
```

`uncovered_by_file` on that artifact is 168 for `src/synthesis_promotion.py` and 123 for `src/codemod_lane.py`, matching the issue and the table above. The same command's defect and churn figures do not match the issue text: `synthesis_promotion.py` ranked 56th with escaped 3.0 and churn 11; `codemod_lane.py` ranked 41st with escaped 5.0 and churn 9. The lookback ends at the clock of this run, not at the 2026-10-19 dispatch, and removing the issue's reserved-file list still leaves `src/feedback.py` and other modules ahead of both targets. The uncovered-statement counts are the part of the original ranking this artifact reproduces. The October dispatch order is the contract's stated order; this later rank does not select those two files first.

## Historical gaps

- No new CI pytest run and no new Gate run were executed for this evidence.
- The merge revision's own pytest job failed the floor equality. The passing pytest job is the PR-head run above.
- The parent SHA has no CI run. The before coverage artifact is the previous main push, which differs only by `.verify-floor.json`.
- Gate workflow runs for the PR head and the merge retain zero artifacts, so Gate's python-ci coverage stats and summary are unavailable.
- Branch coverage was disabled, so uncovered branches and any branch-only reduction are unmeasured.
- Local coverage used coverage.py 7.17.2 and a subset of the suite. It is not a substitute for the 7.16.2 combined artifacts.
