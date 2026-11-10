# Coverage round 1182 recovery

Checked 2026-11-07. PR #1226 retains `agents/codex-1205.md`. Recovery source:
`515371bd806b1b297704b3fef796274a216e6bde:tests/test_coverage_round_1182.py`.
Main was merged at `010e357f08b319867f2699f46f447293c73ba31c` (main parent
`98f5b213`), preserving its existing tests. `rg -n
'test_ccusage_reconcile_uses_loaded_sessions|test_build_window_acquisition_blocked' tests`
found neither symbol before recovery. All 22 deferred functions are now in owning test files:
7 ccusage, 10 exploration, and 5 dashboard tests. The original mode-selection test stays unchanged.
The shard manifest registers the new files; no dependency pins or collection floor were edited.

## Focused verification

Interpreter: `/opt/anaconda3/bin/python3`, Python 3.12.2, pytest 9.1.1,
coverage 7.16.2. Command for all three recovered chunks:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -q --no-cov
```

Result after final test-file formatting: `22 passed in 3.50s`. Independent chunk commands:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_ccusage_reconcile.py -q --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_exploration_collection.py -q --no-cov
/opt/anaconda3/bin/python3 -m pytest tests/test_observability_dashboard.py -q --no-cov
```

They printed `7 passed in 0.77s`, `10 passed in 1.85s`, and `5 passed in 0.97s`, respectively.
Logs are retained alongside coverage output. Initial recovery yielded 21 passed / 1 failed because the dashboard
selftest accessed an import-captured live capability-registry path. The recovered fixtures now
use the existing `advisor_hermetic.activated` helper with a per-test temporary root, repointing
captured paths and defaults. External process/network calls remain stubbed or forbidden.
No production change was needed. Mutation failures and restored passes are recorded in
[COVERAGE_ROUND_1182_MUTATIONS.md](COVERAGE_ROUND_1182_MUTATIONS.md).

## Comparable focused branch measurement

Same source, configuration, collected test files, interpreter and branch instrumentation.
Baseline runs only the preserved pre-existing test; final additionally executes the recovered
22 tests. This measures the focused test contribution, **not whole-tree incremental coverage**:
existing selftests elsewhere in CI already exercise many of these statements.

```sh
COVERAGE_FILE=/tmp/1182-baseline.data /opt/anaconda3/bin/python3 -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -k test_choose_mode_aliases_and_deficits -q --cov=ccusage_reconcile --cov=exploration_collection --cov=observability_dashboard --cov-branch --cov-report=json:/tmp/1182-baseline.json
COVERAGE_FILE=/tmp/1182-final.data /opt/anaconda3/bin/python3 -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -q --cov=ccusage_reconcile --cov=exploration_collection --cov=observability_dashboard --cov-branch --cov-report=json:/tmp/1182-final.json
```

The recorded baseline invocation omitted the `COVERAGE_FILE` assignment; it affects only the
data destination. Results: baseline 1 passed / 22 deselected; final 23 passed in 9.90s.
Raw JSON and printed logs: [verification/coverage-round-1182](verification/coverage-round-1182).

| Target | Baseline uncovered statements | Final uncovered statements | Baseline uncovered branches | Final uncovered branches | Reduction in units |
|---|---:|---:|---:|---:|---:|
| `src/ccusage_reconcile.py` | 242 | 7 | 72 | 2 | 305 |
| `src/exploration_collection.py` | 269 | 0 | 105 | 3 | 371 |
| `src/observability_dashboard.py` | 660 | 8 | 238 | 18 | 872 |

Focused reduction: (242+269+660+72+105+238) - (7+0+8+2+3+18) = 1548 units.
Focused shortfall against 180 = max(0, 180-1548) = 0. This does not establish the issue's
whole-tree goal; only a comparable Gate measurement can do that.

## Authoritative CI / whole-tree measurement

Baseline CI: [42369596492](https://github.com/stranske/Orchestrator/actions/runs/42369596492),
head `643708a208b9303d94d51f74405176fb433eb78e`. All six pytest shards passed; aggregate
failed because the collection floor was behind main (14884 collected versus 14788).
Its `gate-coverage` artifact is retained as `baseline-gate.json` above. Per-target missing
statements: ccusage 60, exploration 60, dashboard 61; total 181.
This artifact has `branch_coverage: false`: branch counts are unavailable, not zero.
The three production targets are unchanged between this baseline and the recovery branch.

Recovery/test commit: `0e4e30164647f2436f7992ab086101c39c2a4634`.
Authoritative CI: [42378820096](https://github.com/stranske/Orchestrator/actions/runs/42378820096),
exact recovery head above. All pytest shards passed. The `gate-coverage` artifact
`coverage.json` reports per-target missing statement counts from this run against the
baseline 60 / 60 / 61:

| Target | Final missing statements | Baseline | Reduction |
|---|---:|---:|---:|
| `src/ccusage_reconcile.py` | 7 | 60 | 53 |
| `src/exploration_collection.py` | 0 | 60 | 60 |
| `src/observability_dashboard.py` | 5 | 61 | 56 |

Total whole-tree statement reduction: 169 units (181 baseline to 12 final).
Statement shortfall against the approximately 180-unit goal: 11 units.
Whole-tree branch reduction is unavailable due to `branch_coverage: false` in CI.

CI's repository-selected Python 3.12 runs:

```sh
python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag
```

Bare `python3 src/verify.py --coverage` runs the whole local suite, selftests, and gates.
The historical CI artifacts above establish a 169-statement reduction and an 11-unit
shortfall. They do not establish whole-tree branch reduction. See the continuation
below for the current local invocation and its missing-dependency failure.

Stop rule: two attempts per failing verification step or 75 minutes total. No unrelated repairs.

## 2026-11-10 continuation: isolation checks and verification blocker

An AST comparison against the original file fetched at
`515371bd806b1b297704b3fef796274a216e6bde` confirmed that all 23 original test
functions were already present with identical bodies before this continuation.
The existing three recovered chunks contain the 22 deferred tests.
Gate [42825030539](https://github.com/stranske/Orchestrator/actions/runs/42825030539)
and CI [42825029936](https://github.com/stranske/Orchestrator/actions/runs/42825029936)
both succeeded on `c0b6736875c3dc41556fe082b9e61939fc97d052`.
Those results precede the uncommitted changes below.

The recovered fixtures now explicitly place `adapters.HANDOFF` and `adapters.LEDGER`
under each test's `tmp_path`. `HANDOFF` otherwise defaults to a directory outside
`ORCH_LOCAL_RUNTIME`, so the existing runtime-root remapping did not isolate it.
The loaded-session test additionally checks a nonempty loader result, the forwarded
lookback, strict failure, and zero writes. The write-path test now reads the temporary
database to verify that a dry run writes nothing and an applied run persists the expected cost.

This runner has Python 3.12.16 but no pytest, pytest-cov, coverage, or Black.
Installing them from the package index failed because DNS/network access is unavailable;
the local package cache contains none of them. Actual command results:

```text
/tmp/orch-1182-venv/bin/python -m pytest tests/test_coverage_round_1182.py tests/test_ccusage_reconcile.py tests/test_exploration_collection.py tests/test_observability_dashboard.py -q --no-cov -m 'not slow'
/tmp/orch-1182-venv/bin/python: No module named pytest
```

```sh
PYTEST_ADDOPTS='-m "not slow"' ORCH_STATE_DIR=/tmp/orch-1182-verify/state ORCH_LOCAL_RUNTIME=/tmp/orch-1182-verify/runtime HANDOFF_DIR=/tmp/orch-1182-verify/handoff /tmp/orch-1182-venv/bin/python src/verify.py --coverage
```

The verify command exited 1, collected zero tests, reported `No module named coverage`
for instrumented children, and ended with:

```text
coverage: NO DATA — no instrumented child wrote a data file (did any test run?)
```

Formatting and the required repository-wide Black check also exited 1 with
`No module named black`. No commit was made because the pre-commit formatting gate
could not pass. Direct Python smoke checks passed the new loaded-session assertions,
the dry-run/persisted-cost assertions, and temporary handoff/ledger writes; these do
not establish a focused pytest pass or a new coverage measurement. Syntax parsing and
`git diff --check` passed. No task checkbox is completed by this continuation.
Attempts to post the blocker comment and add `needs-human` to PR #1323 were both
rejected by the GitHub tools: `MCP tool call requires approval, but approval policy
is never`. Neither remote change was applied. The PR remains open with `draft: false`.

The earlier measured whole-tree statement reduction remains 169 units with an
11-unit shortfall.
Whole-tree branch reduction remains unavailable because the historical Gate artifact
did not instrument branches. No new per-target coverage counts are claimed here.
