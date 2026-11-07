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

Result: 22 passed. Initial recovery yielded 21 passed / 1 failed because the dashboard
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

Final exact-head CI and Gate comparison: pending first recovery push. No CI success is claimed.
CI's repository-selected Python 3.12 runs:

```sh
python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag
```

Bare `/opt/anaconda3/bin/python3 src/verify.py --coverage` runs the whole local suite,
which the continuation explicitly forbids. Local aggregate mode also runs all selftests and
gates. Consequently full verification is assigned to CI, not silently run locally.
The approximately 180-unit whole-tree goal and its shortfall remain UNKNOWN until final artifacts.

Stop rule: two attempts per failing verification step or 75 minutes total. No unrelated repairs.
