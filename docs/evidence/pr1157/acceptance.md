# PR #1157 repair acceptance — 2026-11-05

Outcome 1: all eight new admission tests execute in native CI and final-head checks pass.
Command: `gh pr checks 1157 --repo stranske/Orchestrator`.
Local discriminating check: `/opt/anaconda3/bin/python3 -m pytest -q tests/test_capability_admission_failure_paths.py`.
Stop rule: three unsuccessful repair attempts or 60 minutes, then report the first broken step.

Initial real-input result: admission module prints `8 passed in 0.54s`.
The exact CI failure reproduces with `/opt/anaconda3/bin/python3 -m pytest -q tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once`:
`AssertionError: unassigned modules: ['tests/test_capability_admission_failure_paths.py']; stale modules: []` (`1 failed in 26.57s`).
Coverage run 42021537709 shard 1 reports the same failure; its aggregate does not publish coverage because shard 1 has no data.
Blocking/drainable: one unassigned module / one manifest entry; adding it drains the failure while all existing floors and skip ceilings remain enforced.

After adding the module to `config/test-shards.json`, the same two focused checks print `9 passed in 11.89s`. No full suite was run locally (the partition check collects it).

All eight actual production mutations and their exact-test assertion failures/restored passes are in [mutations.json](mutations.json). Every break exited 1 with a named AssertionError; every restored exact test exited 0 with `1 passed`. The consumer mutation restores the exact base-branch regex, including its original `[A-Za-z0-9_]` initial character class.

Consumer-token bug: the old regex starts matching only at an alphanumeric/underscore, so `../outside.py:consume` is silently reduced to `outside.py:consume` before confinement validation. Allowing path punctuation in the initial character class preserves the declaration for the existing resolver to reject intact. This is the branch's sole production fix and is preserved unchanged.

Combined baseline: native CI run 42001323853 at the PR base c39bdadb, artifact gate-coverage/coverage.json. It measures statements, with branch_coverage=false; no branch units may be claimed. Totals and ranked-target missing lines are retained in [coverage-before.json](coverage-before.json). Admission has 48 missing statements; local_verify already has zero (the issue's older 16-line list is stale); adapters has 143 (older issue: 152). Final native CI coverage and the numerical delta/shortfall will be recorded in the PR body after this push.

Partial scope: admission is the highest ranked target and the eight tests exercise its failure paths. local_verify needs no additional coverage at the actual base (100%). Adapters still has 143 missing statements, but adapter-specific tests and mutations are outside this continuation's explicit repair scope; no claim is made that those paths are untestable. The owner explicitly allows a partial result, so no unrelated coverage padding is added.
