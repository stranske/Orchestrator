# Coverage round 1182 recovery

Checked 2026-11-10. Source recovery commit:
`515371bd806b1b297704b3fef796274a216e6bde:tests/test_coverage_round_1182.py`.
Validated test/source head: `e7f8daef8b9f12f2f5d5bb1900746253e4224381`.
This continuation changes documentation and evidence only; the tests are already delivered.
Issue #1182 was found closed and reopened; this PR does not close it.

## Recovery and isolation

AST comparison against the original git blob found all 23 original test names
exactly once: the preserved mode-selection test plus 7 ccusage, 10 exploration,
and 5 dashboard tests in their owning files. Twenty original bodies remain
identical; the loaded-session, persisted-cost and acquisition-blocked tests have
additional assertions. The shard manifest includes the recovered files.

The recovered fixtures activate `advisor_hermetic` under `tmp_path`, repoint
captured runtime paths and feedback databases, and explicitly isolate adapters'
HANDOFF and LEDGER. Subprocess probes return offline stubs; unstubbed process
or socket calls fail the test. Selftest temporary directories are forced beneath
the test root. The write-path test checks a temporary database: dry-run writes
nothing, while apply persists the expected cost. No production edits or dependency
pin changes were needed.

Success outcome: all 23 recovered/preserved tests execute and pass on real module
inputs while the fixtures prevent external I/O and isolate writes. Stop rule for
this continuation: three failed repair attempts at the same step or 60 minutes.

## Focused local verification

Runtime: `/opt/anaconda3/bin/python3`, pytest 9.2.0, coverage 7.16.2.
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

Both commands use the same source, four test files, interpreter, configuration
and branch instrumentation. Baseline selects the preserved mode-selection test;
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

## Authoritative CI and comparable whole-tree Gate measurement

Baseline [CI 42369596492](https://github.com/stranske/Orchestrator/actions/runs/42369596492),
head `643708a208b9303d94d51f74405176fb433eb78e`: six pytest shards passed;
aggregate failed from a lagging collection floor (14884 collected vs 14788).
The retained `baseline-gate.json` is its measured coverage, not a passing aggregate verdict.

Current [CI 42828478565](https://github.com/stranske/Orchestrator/actions/runs/42828478565)
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

Whole-tree statement reduction: 181 - 12 = **169 units**. Shortfall against the
approximately 180-unit goal: **11 units**. Both Gate JSON artifacts explicitly
have `branch_coverage: false`; whole-tree branch reduction is unknown, not zero.
The comparable branch measurement above is focused only. The approximately
180-unit whole-tree goal is not established as achieved.

## Required verify invocation

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

## Concurrent isolation strengthening preserved

Before push, remote commit `ec4c41c5eeb8b991f49e0fbf952e1133cc586a04`
added HANDOFF_DIR isolation, guards on socket connect/connect_ex, and ledger
assertions exercising the fixture's actual captured paths. Those changes were
preserved by rebasing; the focused commands and both mutations were then rerun.
The measurements and aggregate run above identify their original tested head;
production target files are unchanged by the concurrent test-only commit.
