# Coverage round 1182 recovery

## Current delivered-tree evidence (2026-11-10)

**Outcome: 23 recovered/preserved tests pass on the delivered tree.**
PR [#1323](https://github.com/stranske/Orchestrator/pull/1323) merged as
`17a7c031f24a1a92f9c9e3e90320d5c1c2c13574`. This evidence-only repair starts
from main at `c12f08149f0973882979e46be5f4bdd129175a9f` (tree
`787522cebc596a8b8a15c8fcf78b887c296fa123`). It preserves the merged tests.

The acceptance outcome, commands, runtime and stop rule were recorded before
execution in [delivered-acceptance.txt](verification/coverage-round-1182/delivered-acceptance.txt).
The stop rule is three failures at one step or 30 minutes. All four runs passed
on the first attempt; no test or production repair was needed. No repository
virtual environment was selected, so the runtime was `/opt/anaconda3/bin/python3`
(Python 3.12.2, pytest 9.2.0). No full local suite or verify pipeline was run
for this repair.

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -vv --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py -vv --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_exploration_collection.py -vv --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_observability_dashboard.py -vv --no-cov
```

Complete named output, command, source SHA/tree, runtime, exit code and wall time:

| Run | Pytest result | Wall time | Transcript |
|---|---|---:|---|
| Preserved test plus all recovered tests | 23 passed in 4.28s | 5.340s | [delivered-focused.log](verification/coverage-round-1182/delivered-focused.log) |
| ccusage chunk | 7 passed in 0.67s | 1.687s | [delivered-ccusage-chunk.log](verification/coverage-round-1182/delivered-ccusage-chunk.log) |
| exploration chunk | 10 passed in 2.19s | 3.213s | [delivered-exploration-chunk.log](verification/coverage-round-1182/delivered-exploration-chunk.log) |
| dashboard chunk | 5 passed in 0.89s | 1.914s | [delivered-dashboard-chunk.log](verification/coverage-round-1182/delivered-dashboard-chunk.log) |

These runs cover the final CLI/isolation assertions, including
`test_exploration_collection_main_paths` (private router HANDOFF/DECISION_JSON,
active decision persistence, each independent dispatch blocker preserving that
decision, no ledger creation) and `test_dashboard_markdown_and_main` (JSON file
matching stdout, Markdown matching rendering, `--no-capacity` forbidding a live
probe, no ledger creation). They also name the two mutation-sensitive tests:
`test_ccusage_reconcile_uses_loaded_sessions` and
`test_build_window_acquisition_blocked`. The restored mutation evidence remains
in [COVERAGE_ROUND_1182_MUTATIONS.md](COVERAGE_ROUND_1182_MUTATIONS.md).

Final-head [CI 42849835806](https://github.com/stranske/Orchestrator/actions/runs/42849835806)
and [Gate 42849836260](https://github.com/stranske/Orchestrator/actions/runs/42849836260)
both completed successfully on #1323 head
`d715b7d9d1542f5952ba5ad0cf21123af62a6ec3`, before its merge. All four test
files are byte-identical between that head and the delivered main source SHA
above. The new local runs validate the delivered main tree, including its
changed shared conftest; they do not relabel the older coverage artifacts as
final-head measurements.

## Historical opening blocker (superseded by merge and results above)

An earlier 2026-11-10 automation workspace recovered all 23 original names from
`515371bd806b1b297704b3fef796274a216e6bde` and strengthened the CLI/isolation
assertions. Its Python 3.14.10 lacked pytest and Black, and package installation
failed on DNS resolution. It reported failed validation and approval-rejected
GitHub writes while #1323 was still open. Those were that workspace's historical
blockers, not the state of this delivered tree. The test changes are now merged;
there is no open #1323 or uncommitted test continuation. Its syntax/smoke checks
were not pytest or coverage evidence; use the named local runs above for the
final assertions.

## Previously recorded evidence and provenance

Checked 2026-11-10. Source recovery commit:
`515371bd806b1b297704b3fef796274a216e6bde:tests/test_coverage_round_1182.py`.
Validated test/source head: `e7f8daef8b9f12f2f5d5bb1900746253e4224381`.
The older coverage/aggregate measurements below were recorded against that
source head. Historical issue handling recorded #1182 as closed and reopened;
this repair makes no issue-state change.

## Recovery and isolation

AST comparison against the original git blob found all 23 original test names
exactly once: the preserved mode-selection test plus 7 ccusage, 10 exploration,
and 5 dashboard tests in their owning files. That earlier comparison recorded
20 identical original bodies, before the final CLI/isolation assertions described
above; the loaded-session, persisted-cost and acquisition-blocked tests already
had additional assertions. The shard manifest includes the recovered files.

The recovered fixtures activate `advisor_hermetic` under `tmp_path`, repoint
captured runtime paths and feedback databases, and explicitly isolate adapters'
HANDOFF and LEDGER. Subprocess probes return offline stubs; unstubbed process
or socket calls fail the test. Selftest temporary directories are forced beneath
the test root. The write-path test checks a temporary database: dry-run writes
nothing, while apply persists the expected cost. No production edits or dependency
pin changes were needed.

Success outcome: all 23 recovered/preserved tests execute and pass on real module
inputs while the fixtures prevent external I/O and isolate writes. The earlier
continuation used a three-failure/60-minute stop rule;
this repair uses the 30-minute rule recorded above.

## Historical focused local verification

Historical runtime: `/opt/anaconda3/bin/python3`, pytest 9.2.0, coverage 7.16.2.
The initial results were recorded for `e7f8daef8b9f12f2f5d5bb1900746253e4224381`;
the later refreshed logs are qualified in the final section. They are preserved
as historical evidence; the `delivered-*` logs above bind named final assertions
to an immutable source tree.
Commands (no full local pytest suite was run):

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -q --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py -q --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_exploration_collection.py -q --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_observability_dashboard.py -q --no-cov
```

Actual outputs: `23 passed in 6.34s`; individual chunks: `7 passed in 0.88s`,
`10 passed in 2.70s`, `5 passed in 1.90s`. Full logs are retained under
[verification/coverage-round-1182](verification/coverage-round-1182).
The two required behavior-changing mutations failed their named pytest tests,
and passed after byte-for-byte restoration; see
[COVERAGE_ROUND_1182_MUTATIONS.md](COVERAGE_ROUND_1182_MUTATIONS.md).

## Comparable focused statement and branch measurement

This historical comparison was recorded on the source named above; its final
artifact was later refreshed as noted below. The production targets are unchanged.
Both commands use four test files, the same interpreter, configuration and branch
instrumentation. Baseline selects the preserved mode-selection test;
final runs that test plus all 22 recovered tests.

```sh
COVERAGE_FILE=/tmp/1182-baseline-current.data /opt/anaconda3/bin/python3 -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -k test_choose_mode_aliases_and_deficits -q --cov=ccusage_reconcile --cov=exploration_collection --cov=observability_dashboard --cov-branch --cov-report=json:/tmp/1182-baseline-current.json
COVERAGE_FILE=/tmp/1182-final-current.data /opt/anaconda3/bin/python3 -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -q --cov=ccusage_reconcile --cov=exploration_collection --cov=observability_dashboard --cov-branch --cov-report=json:/tmp/1182-final-current.json
```

Baseline: 1 passed, 22 deselected. Final: `23 passed in 12.86s`.
Raw outputs are retained as `baseline-focused.json` and `final-focused.json`.

| Target | Baseline uncovered statements | Final uncovered statements | Baseline uncovered branches | Final uncovered branches | Reduction in units |
|---|---:|---:|---:|---:|---:|
| `src/ccusage_reconcile.py` | 242 | 7 | 72 | 2 | 305 |
| `src/exploration_collection.py` | 269 | 0 | 105 | 3 | 371 |
| `src/observability_dashboard.py` | 660 | 8 | 238 | 18 | 872 |

Focused reduction: 1586 - 38 = 1548 units; focused shortfall against 180:
max(0, 180-1548) = 0. These counts measure the focused contribution; they do
not establish whole-tree incremental coverage, where other tests overlap.

## Historical authoritative CI and comparable whole-tree Gate measurement

Baseline [CI 42369596492](https://github.com/stranske/Orchestrator/actions/runs/42369596492),
head `643708a208b9303d94d51f74405176fb433eb78e`: six pytest shards passed;
aggregate failed from a lagging collection floor (14884 collected vs 14788).
The retained `baseline-gate.json` is its measured coverage, not a passing aggregate verdict.

Earlier [CI 42828478565](https://github.com/stranske/Orchestrator/actions/runs/42828478565)
and [Gate 42828479148](https://github.com/stranske/Orchestrator/actions/runs/42828479148)
both succeeded on `e7f8daef8b9f12f2f5d5bb1900746253e4224381`.
All six pytest shards passed. CI artifact `gate-coverage` (ID 13770636787)
is retained as `current-gate.json`. All three production targets are byte-identical
between the baseline and current heads, with equal statement denominators.

| Target | Statements | Baseline uncovered statements | Current uncovered statements | Reduction | Baseline/current uncovered branches |
|---|---:|---:|---:|---:|---|
| `src/ccusage_reconcile.py` | 285 | 60 | 7 | 53 | unavailable / unavailable |
| `src/exploration_collection.py` | 331 | 60 | 0 | 60 | unavailable / unavailable |
| `src/observability_dashboard.py` | 698 | 61 | 5 | 56 | unavailable / unavailable |

Direct recheck on 2026-11-10:
[delivered-gate-check.log](verification/coverage-round-1182/delivered-gate-check.log)
retains the reproducible parser command, complete output, runtime and artifact
SHA-256 hashes. It loads both retained Gate JSON files, checks each missing-line
list against its summary, and checks equal per-target statement denominators
(285, 331, 698; total 1314). The three production blobs are identical at the
baseline head, earlier measured head, #1323 final head and delivered main head.
The repository-wide denominators differ (120428 vs 77928), so repository-wide
totals are not used to infer this target reduction. `current-gate.json` remains
the earlier CI 42828478565 artifact, not a final-head coverage artifact.

Whole-tree statement reduction for the three targets: 181 - 12 = **169 units**.
Shortfall against the
approximately 180-unit goal: **11 units**. Both Gate JSON artifacts explicitly
have `branch_coverage: false`; whole-tree branch reduction is unknown, not zero.
The comparable branch measurement above is focused only. The approximately
180-unit whole-tree goal is not established as achieved.

## Historical required verify invocation (failed aggregate retained)

Downloaded all six exact-head `pytest-shard-*` artifacts from CI 42828478565.
Their JSON/XML summaries were placed directly in `/tmp/1182-ci-current`, as the
aggregate interface requires; coverage files remain discoverable recursively.
The first attempt failed on the nested download layout (`missing or unreadable
summary for shard 1/6`); the corrected invocation is:

```sh
ORCH_STATE_DIR=/tmp/1182-verify/state ORCH_LOCAL_RUNTIME=/tmp/1182-verify/runtime HANDOFF_DIR=/tmp/1182-verify/handoff /opt/anaconda3/bin/python3 src/verify.py --coverage --aggregate /tmp/1182-ci-current --floor-may-lag
```

This uses authoritative shard verdicts instead of rerunning the full suite locally;
verify still runs its discovered selftests and capability gates under private state.

Actual result: **exit 1**, not a passing local verification. It reused 8530 collected
CI tests: 8501 passed, 0 failed, 27 skipped (plus xfail results); the floor lagged
by 23. Selftests: 104 of 111 ran, 5 skipped, 2 failed (`adapters`, `tick_watchdog`).
The first failure was adapters' default-state-directory assertion; tick_watchdog
failed while creating a subprocess. These are follow-up failures outside the
recovered tests; neither was repaired or suppressed. Full output is retained as
`verify-current.log`. The run took 3m04s for selftests and 48s for gates.

Coverage was still published: **128 instrumented processes combined**. Its
per-target results are retained in `verify-targets.json` (a projection of the
emitted JSON, not a repository-wide totals artifact):

| Target | Local verify uncovered statements | Local verify uncovered branches | Gate uncovered statements |
|---|---:|---|---:|
| `src/ccusage_reconcile.py` | 7 | unavailable | 7 |
| `src/exploration_collection.py` | 0 | unavailable | 0 |
| `src/observability_dashboard.py` | 4 | unavailable | 5 |

Local selftests add coverage to downloaded CI shards, so the local 170-statement
reduction (181 to 11, shortfall 10) is **not** the comparable authoritative Gate
measurement. The accepted comparison remains 169 statements recovered and an
11-unit shortfall. Neither local nor Gate instrumentation measures whole-tree
branches; the focused branch comparison is reported separately above.
The local verification failure remains visible even though the exact-head CI
and Gate verdicts succeeded. No full local pytest suite was run.

## Historical concurrent isolation strengthening preserved

Before push, remote commit `ec4c41c5eeb8b991f49e0fbf952e1133cc586a04`
added HANDOFF_DIR isolation, guards on socket connect/connect_ex, and ledger
assertions exercising the fixture's actual captured paths. Those changes were
preserved by rebasing; the focused commands and both mutations were then rerun.
The measurements and aggregate run above identify their original tested head;
production target files are unchanged by the concurrent test-only commit.

The refreshed focused/chunk and mutation results were recorded after rebasing
onto `ec4c41c5eeb8b991f49e0fbf952e1133cc586a04`, as recorded in
[COVERAGE_ROUND_1182_MUTATIONS.md](COVERAGE_ROUND_1182_MUTATIONS.md).
They precede the final CLI/isolation assertions; use the delivered-tree logs
above for those assertions. Historical results: `23 passed in 3.36s`; chunks `7 passed in
0.56s`, `10 passed in 1.96s`, `5 passed in 0.85s`. The retained focused/chunk
logs and final focused JSON were refreshed after preserving the concurrent changes.
Both named mutations were also re-executed and their retained logs refreshed.
