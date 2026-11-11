## Summary

Adds hermetic tests for the first issue-ranked target, `src/env_prereq.py`. There are now 17 tests: two pytest-style functions and fifteen unittest methods. This iteration tests that `runnable` records only the first missing prerequisite and that `report_gaps` prints each reason once, in first-seen order, with no output when there are no gaps. No production files changed and no production bug was found.

## Reconciliation (2026-11-11)

Reviewed `4cd1748c` (merge from main), `06da00e2` (boundary tests), `61b1d5ee` (isolation and mutation evidence), `9701ef57` (layout regression), `d4234927` (direct selftest contract), and `463c504c` (bare-machine regressions). Before this iteration, the actual PR diff against `f56396f5` contained four files, including shard registration and 15 tests. The reported 35-file previous iteration includes inherited changes; the remote two-test summary is stale.

Verified scope checkboxes are reconciled below. The explicit pytest break-and-revert requirement, CI acceptance, formatting and delivery remain unchecked. The PR-body update was rejected: `MCP tool call requires approval, but approval policy is never`. This is the local handoff copy; the remote body has NOT been updated. The two new tests and this handoff remain uncommitted because the mandatory Black gate cannot run. Earlier iterations were committed by the automation wrapper; this run did not make a commit. Fresh installation into `/tmp/orch-1256-tools` also failed because PyPI DNS resolution is unavailable. PR #1256 was confirmed open and ready for review (`draft=false`).

## Coverage and partial delivery

CI's combined pytest plus selftest measure is authoritative; no full local suite or `verify.py` was run. The linked issue's final “Next attempt” narrows this PR to target 1. Remaining targets are deferred rather than claimed untestable.

| Target | Issue-listed uncovered units | Current evidence |
| --- | ---: | --- |
| `src/env_prereq.py` | 40 | All 17 tests pass using standard-library runners, including its selftest; fresh coverage is unknown. |
| `src/ledger_reconcile.py` | 74 | Outside the latest target-1-only scope. |
| `src/consumer_sync_shadow.py` | 1 | Outside the same scope. |
| `src/improvement_log.py` | 5 | Outside the same scope. |
| `src/spec_provenance.py` | 2 | Outside the same scope. |
| `src/testgen_lane.py` | 18 | Outside the same scope. |
| `src/redirect_plan.py` | 70 | Outside the same scope. |

No fresh covered-unit gain is established, so this partial delivery conservatively leaves the entire approximately 180-unit goal unverified. The two newest tests exercise section-level skip recording and reporting. No additional CI-covered units are claimed without a coverage measurement. No percentage or combined-CI result is inferred from focused test passes.

## Validation and blockers

- The required blocker comment and `needs-human` label were also attempted on PR #1256; both were rejected with `MCP tool call requires approval, but approval policy is never`. Final read-only verification confirms the PR is open and `draft=false`.
- `PYTHONPATH=src:tests python3 -m unittest test_issue1227_env_prereq_coverage.PrerequisiteBoundaryTests -v`: **15 passed**. Both runtime paths, ledger access and home lookup are isolated; process and network calls are denied.
- Both original pytest-style functions pass when called with a standard-library fixture adapter. The first executes `env_prereq._selftest()`, which prints **OK**. This is not a pytest run.
- Both new section-skip tests fail on deliberate mutations to temporary source copies, and pass after restoring the copies. Repository production source is unchanged. Full output is appended to `mutation-evidence.txt`.
- `git diff --check`: passed.
- `python3 -m pytest tests/test_issue1227_env_prereq_coverage.py --cov=env_prereq --cov-branch --cov-report=term-missing -m "not slow"`: blocked, **No module named pytest**.
- `python3 -m black --line-length 100 tests/test_issue1227_env_prereq_coverage.py` and `python3 -m black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`: blocked, **No module named black**.
- `python3 -m ruff check --extend-exclude .workflows-lib --output-format concise tests/test_issue1227_env_prereq_coverage.py`: blocked, **No module named ruff**.
- Installation into `/tmp/orch-1256-venv` failed because `pypi.org` cannot be resolved. Searches of local Python installations and caches found no usable copies of the required tools.

## Break-and-revert evidence

Earlier evidence records all 15 previous tests failing on deliberate source mutations and passing after restoration, using unittest or a standard-library fixture adapter. Those runs are preserved in `mutation-evidence.txt`; they do not establish the explicit pytest requirement.

Fresh runs for this iteration used `python3 -m unittest <test-id> -v` with temporary `env_prereq.py` copies placed first on `PYTHONPATH`. Each broken run exited 1 with assertion failures; each restored run exited 0. Every case also used the fixture's temporary runtime and external-I/O denials.

| Test | Deliberate mutation | Broken / restored |
| --- | --- | --- |
| first missing prerequisite controls whether a section runs | `runnable` returns True after recording a missing prerequisite | `True is not false`, exit 1 / OK, exit 0 |
| gap reporting preserves unique first-seen reasons | `dict.fromkeys(gaps)` changed to `gaps` | duplicate output line, exit 1 / OK, exit 0 |

Closes #1227

<!-- auto-status-summary:start -->
## Automated Status Summary
#### Scope
Orchestrator reads 87.19% blended line+branch in CI; reaching 90% needs about 3981 more covered statements+branches. Prioritize modules with escaped defects or low combined coverage; tests only.

The target order below is the Orchestrator's own ranking (escaped defects, then churn, then uncovered mass -- unchanged). Only the bundling is new: earlier rounds spent a whole PR on files with a handful of uncovered statements, so one PR now covers the next ranked targets until it has covered about 180 units.

<!-- Updated WORKFLOW_OUTPUTS.md context:start -->
## Context for Agent

### Related Issues/PRs
- [#1228](https://github.com/stranske/Orchestrator/issues/1228)
<!-- Updated WORKFLOW_OUTPUTS.md context:end -->

#### Tasks
- [x] "Uncovered units" here come from the repository's whole CI run (every test file together). One test module's own suite can already cover more than CI's count shows for that module, and CI's figure is the one this PR is judged on: if your baseline differs, say so in the PR body and use CI's number.
- [ ] Before you push, run the repository's own formatter and linter on every file you add or change, with the flags its CI `lint-format` and `lint-ruff` jobs use: one unformatted file fails a required check, and waiting for the autofix bot to repair it afterwards has not worked on lane PRs.
- [ ] Deliver what you verified. If you cannot reach the goal for ANY reason (a broken local environment, a target you could not finish, the time you had), still open the pull request with the verified work you have, say in its body how far short it is and why, and never leave verified commits or edits unpushed or uncommitted: a partial pull request that merges is worth more than a complete one that never opens, and a worktree left holding work blocks this issue's next dispatch.
- [x] This repository's coverage is a COMBINED measure: pytest plus every module's `--selftest`, run as subprocesses in coverage's parallel mode, so lines that only a selftest executes already count as covered. A plain `pytest --cov` baseline therefore reads lower than CI. The PR's CI coverage is the authority. Do NOT run `python3 src/verify.py --coverage`, any other `python3 src/verify.py` run, or the whole test suite locally: serially it takes over an hour on this machine and slows the mirror sync and every other lane (owner rule 2026-11-08). Locally run only the test modules you add or change, plus the `--selftest` of the modules they cover.
- [x] Do NOT edit `.verify-floor.json` in this PR. CI runs `verify.py --floor-may-lag` on pull requests, so a floor that is behind the tests you add is reported and forgiven (a collection DROP still fails), and main's reconcile-floor job writes the measured counts after the merge. Editing the file is what makes lane PRs conflict each time main moves it. Never raise a skip ceiling to make room.
- [x] Tests must be hermetic: set BOTH `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` to a temp dir, stub every `subprocess`/`Popen`/agent spawn and every `gh` or network call and assert nothing was started, and never write outside that temp dir. A test that can start an agent or touch the live ledger is a defect.
- [x] This is a tests-only round: change no `ARCHITECTURE.md`, capability ledger row or runtime flag. Black runs at line length 100.
- [x] Do not merge: the closer lane merges this repository's PRs through `merge_guard.py`.

#### Acceptance criteria
- [ ] `pytest` passes in CI with the new tests, and each new test fails when the code it covers is deliberately broken (the PR body shows the break-and-revert run).
- [x] The PR's Gate coverage shows the targets' uncovered statements+branches down by about 180 or more, or the PR body states how far short it is and why and explains each target that could not be meaningfully covered (a partial PR meets this criterion; no PR does not).
- [x] Production code changes only where a test found a real bug, each one explained in the PR body.

- [ ] Same contract as every round in this initiative:
1. [x] Pick the next target by the repo's own coverage-gap ranking (escaped-defect priority, then
   churn, then uncovered mass), not by file size.
2. [ ] Every new test must FAIL when the code it covers is deliberately broken and PASS when
   reverted -- run that break-and-revert for real and report it, don't assert it.
3. [ ] Real bugs found while writing tests get fixed with a minimal, targeted diff in the same PR,
   not worked around. Watch for the negative-cache pattern (cache.get(key) is None used as a 'not yet cached' check, which can't tell 'never queried' from 'queried and got nothing') -- it has recurred independently four separate times fleet-wide already.
4. [x] Low blast radius only -- test-only or tightly-scoped fixes, no refactors.
5. [x] One PR per logical chunk of work.

- [ ] The goal is to improve the code, not a metric. If you measure this repo at or above 90% before
- [ ] starting, say so in a comment on this issue and close it without opening a PR -- do not
- [ ] manufacture a fix just because a round was requested.

<!-- auto-status-summary:end -->
