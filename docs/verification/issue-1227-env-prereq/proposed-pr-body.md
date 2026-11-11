## Summary

Adds hermetic tests for the first issue-ranked target, `src/env_prereq.py`. The module now contains 12 tests: two pytest-style functions and ten unittest methods. This round adds installed-skill and Codex-resolver contracts, blocks subprocess and network access in the original functions, and confines selftest temporary files to the test's temporary root. No production files changed.

## Reconciliation

Reviewed `4cd1748c` (merge from main) and `06da00e2` (eight additional boundary tests). Before this round, the actual PR diff against base `f56396f5` contained only the test module and its explicit shard registration. The reported 33-file prior round included inherited changes. The old two-test validation was stale.

Verified scope and mutation-proof items are checked below. CI acceptance remains unchecked. This is a proposed body saved locally: GitHub rejected the update because its tool requires approval and this run's approval policy is `never`. The new test edits are uncommitted because the mandatory Black gate could not run.

## Coverage and partial delivery

CI's combined pytest plus selftest measure is authoritative; no full local suite or `verify.py` was run. The issue-listed targets remain in their supplied ranking:

| Target | Issue-listed uncovered units | Current evidence |
| --- | ---: | --- |
| `src/env_prereq.py` | 40 | 12 tests pass using standard-library runners, including its selftest; fresh coverage is unknown. |
| `src/ledger_reconcile.py` | 74 | Deferred after the first target: required test and formatting tools are unavailable. |
| `src/consumer_sync_shadow.py` | 1 | Deferred for the same verification blocker. |
| `src/improvement_log.py` | 5 | Deferred for the same verification blocker. |
| `src/spec_provenance.py` | 2 | Deferred for the same verification blocker. |
| `src/testgen_lane.py` | 18 | Deferred for the same verification blocker. |
| `src/redirect_plan.py` | 70 | Deferred for the same verification blocker. |

No fresh covered-unit gain is established, so this partial delivery conservatively leaves the entire approximately 180-unit goal unverified. The remaining targets are deferred, not claimed untestable. No percentage or combined-CI result is inferred from focused test passes.

## Validation and blockers

- `PYTHONPATH=src:tests python3 -m unittest test_issue1227_env_prereq_coverage.PrerequisiteBoundaryTests -v`: **10 passed** with both runtime paths set to a temporary directory.
- Both original pytest-style functions pass when called with a standard-library fixture adapter. The first executes `env_prereq._selftest()`, which prints **OK**. This is not a pytest run.
- Each of the 12 tests fails with a deliberate source mutation and passes after restoring the source. Mutations were applied to temporary copies; repository production source was unchanged.
- `git diff --check`: passed.
- `python3 -m pytest tests/test_issue1227_env_prereq_coverage.py --cov=env_prereq --cov-branch --cov-report=term-missing -m "not slow"`: blocked, **No module named pytest**.
- Formatting the changed file and the mandatory repository-wide `black --check --line-length 100 --exclude '(\\.workflows-lib|node_modules)' .`: blocked, **No module named black**.
- `python3 -m ruff check --extend-exclude .workflows-lib tests/test_issue1227_env_prereq_coverage.py`: blocked, **No module named ruff**.
- Installation into a temporary virtual environment failed because PyPI DNS/network access is unavailable. No cached copies of the missing tools were found.

## Break-and-revert evidence

All runs used the actual test code and temporary copies of `env_prereq.py`. Each restored invocation exited zero. The ten class methods ran through `python3 -m unittest <test-id> -v`; the two functions used the standard-library fixture adapter. Evidence is preserved in `mutation-evidence.txt` beside this proposed body.

| Test | Deliberate source mutation | Broken / restored |
| --- | --- | --- |
| isolated-runtime selftest | `bare_machine` OR composition changed to AND | assertion failed / passed |
| unreadable-ledger detectors | first unreadable-ledger reason changed to `None` | assertion failed / passed |
| core sibling | present-runtime branch forced false | FAIL / OK |
| populated lineage | populated-lineage branch forced false | FAIL / OK |
| legacy rows | legacy-presence branch forced false | FAIL / OK |
| git detector | success/error exit-code comparison inverted | FAIL / OK |
| seat signals | CLI-or-file composition changed to AND | FAIL / OK |
| recorded verdicts | valid-verdict filter forced false | FAIL / OK |
| replayed failure | required failure message replaced | FAIL / OK |
| CLI report | required checkout/mirror label replaced | FAIL / OK |
| installed skill (new) | installed-resource branch forced false | FAIL / OK |
| Codex resolver (new) | successful resolver result changed to an absence reason | FAIL / OK |

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
- [x] Before you push, run the repository's own formatter and linter on every file you add or change, with the flags its CI `lint-format` and `lint-ruff` jobs use: one unformatted file fails a required check, and waiting for the autofix bot to repair it afterwards has not worked on lane PRs.
- [x] Deliver what you verified. If you cannot reach the goal for ANY reason (a broken local environment, a target you could not finish, the time you had), still open the pull request with the verified work you have, say in its body how far short it is and why, and never leave verified commits or edits unpushed or uncommitted: a partial pull request that merges is worth more than a complete one that never opens, and a worktree left holding work blocks this issue's next dispatch.
- [x] This repository's coverage is a COMBINED measure: pytest plus every module's `--selftest`, run as subprocesses in coverage's parallel mode, so lines that only a selftest executes already count as covered. A plain `pytest --cov` baseline therefore reads lower than CI. The PR's CI coverage is the authority. Do NOT run `python3 src/verify.py --coverage`, any other `python3 src/verify.py` run, or the whole test suite locally: serially it takes over an hour on this machine and slows the mirror sync and every other lane (owner rule 2026-11-08). Locally run only the test modules you add or change, plus the `--selftest` of the modules they cover.
- [x] Do NOT edit `.verify-floor.json` in this PR. CI runs `verify.py --floor-may-lag` on pull requests, so a floor that is behind the tests you add is reported and forgiven (a collection DROP still fails), and main's reconcile-floor job writes the measured counts after the merge. Editing the file is what makes lane PRs conflict each time main moves it. Never raise a skip ceiling to make room.
- [x] Tests must be hermetic: set BOTH `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` to a temp dir, stub every `subprocess`/`Popen`/agent spawn and every `gh` or network call and assert nothing was started, and never write outside that temp dir. A test that can start an agent or touch the live ledger is a defect.
- [x] This is a tests-only round: change no `ARCHITECTURE.md`, capability ledger row or runtime flag. Black runs at line length 100.
- [x] Do not merge: the closer lane merges this repository's PRs through `merge_guard.py`.

#### Acceptance criteria
- [ ] `pytest` passes in CI with the new tests, and each new test fails when the code it covers is deliberately broken (the PR body shows the break-and-revert run).
- [ ] The PR's Gate coverage shows the targets' uncovered statements+branches down by about 180 or more, or the PR body states how far short it is and why and explains each target that could not be meaningfully covered (a partial PR meets this criterion; no PR does not).
- [x] Production code changes only where a test found a real bug, each one explained in the PR body.

- [ ] Same contract as every round in this initiative:
1. [x] Pick the next target by the repo's own coverage-gap ranking (escaped-defect priority, then
   churn, then uncovered mass), not by file size.
2. [x] Every new test must FAIL when the code it covers is deliberately broken and PASS when
   reverted -- run that break-and-revert for real and report it, don't assert it.
3. [ ] Real bugs found while writing tests get fixed with a minimal, targeted diff in the same PR,
   not worked around. Watch for the negative-cache pattern (cache.get(key) is None used as a 'not yet cached' check, which can't tell 'never queried' from 'queried and got nothing') -- it has recurred independently four separate times fleet-wide already.
4. [x] Low blast radius only -- test-only or tightly-scoped fixes, no refactors.
5. [x] One PR per logical chunk of work.

- [ ] The goal is to improve the code, not a metric. If you measure this repo at or above 90% before
- [ ] starting, say so in a comment on this issue and close it without opening a PR -- do not
- [ ] manufacture a fix just because a round was requested.

<!-- auto-status-summary:end -->

