# Sharded verification

Every PR runs six four-worker pytest shards in the checkout and six in the flat
exec mirror. Each shape has one aggregate that runs the existing selftests and gates.
The checkout aggregate also combines pytest-worker, selftest and gate coverage and
publishes `gate-coverage` and `gate-coverage-trend`.

The Gate's reusable Python CI uses this same module plan for both Python 3.13 and
3.14. Its six caller-matrix entries pass the module paths through `pytest_args`.
Coverage is disabled on every Gate caller: each caller sees only one sixth of the
suite, so its percentage is not a whole-tree measurement. Each call uses a
shard-specific artifact prefix, and the Gate summary requires all twelve
shard/runtime receipts even though their coverage checks are explicit skips.

Whole-tree coverage has one source: the checkout `verify.py aggregate + combined
coverage` job. It requires coverage data from every checkout shard, combines that
data with the selftest and gate subprocess measurements, and publishes the
coverage.py payload as `gate-coverage/coverage.json` plus the matching
`gate-coverage-trend/coverage-trend.json`. The Gate summary locates those two
artifacts on the exact-head CI run, validates that both percentages agree, and
re-publishes the same artifact names and schemas. A missing payload, missing trend,
invalid percentage, or payload/trend mismatch is reported as a distinct `UNKNOWN`
sentinel; none is converted to zero or pass. Maint Coverage Guard already names
`ci.yml` as its source in `config/coverage-baseline.json`, so its baseline and trend
remain bound to this combined measurement.

Counts come from the final **printed pytest summary** of each shard. JUnit supplies
gate replay verdicts and skip reasons, never counts. Every summary must be present
and match the shard plan and tree shape. The original collection equality, pass
floor and shape-specific skip ceilings apply to the summed counts. The checkout
allows a lagging floor only on PRs; the mirror retains its existing allowance on
every event. Neither permits a collection drop.

Run a shard and then aggregate its evidence with its peers:

```sh
VERIFY_PYTEST_WORKERS=4 python3 src/verify.py --pytest-shard 1/6 --coverage --shard-output shard-results
# Collect shard-1.json through shard-6.json and every .coverage.shard-*.*
# from the separate shard checkouts into one evidence directory.
python3 src/verify.py --aggregate evidence --coverage --floor-may-lag
```

Use separate checkouts for concurrent instrumented shards: each invocation clears
its checkout's stale coverage data. Shard evidence records contain the printed
output, exit status, gate verdicts, skip reasons and a digest of the module plan.
A shard's successful exit is partial evidence; it cannot publish a verified mirror
snapshot. The aggregate supplies the whole-tree verdict.

When adding or removing a test module, regenerate the plan:

```sh
python3 -m pytest -q -n 4 --junitxml=junit.xml
python3 tools/make_test_shards.py junit.xml --shards 6 --workers 4
python3 -m pytest -q tests/test_verify_sharding.py
```

The generator balances modules by summed JUnit timing divided by the worker count
(four on CI), with deterministic ties. It lists modules absent from the timing
sample and assigns them the median measured duration. The current plan uses hosted
PR CI run `41626212635`: JUnit from all six checkout `pytest-shard-*` artifacts
merged into one timing sample (`timing_source: merged-junit.xml` in the manifest).
JUnit is used here only for balancing timings. Hosted job minutes and pipeline
wall time are read separately from `gh run view --json jobs`.

## CI workflow scope and measured job durations

Workflow: `.github/workflows/ci.yml` on `pull_request` (15 jobs: six checkout
shards, checkout aggregate, one flat-mirror prebuild, six flat-mirror shards,
flat-mirror aggregate; `reconcile the collection floor` is push-to-main only).

Critical path for issue #957: earliest start among all shard jobs through latest
completion among the two aggregate jobs (`verify.py aggregate + combined coverage`
and `verify.py aggregate in the flat exec-mirror shape`), including the flat mirror
leg.

### Baseline before this repair (five latest successful PR CI runs on 2026-11-03)

| Run ID | Earliest shard start (UTC) | Latest aggregate completion (UTC) | Critical path (s) | Max job (s) | Median job (s) |
|--------|----------------------------|-----------------------------------|-------------------|-------------|----------------|
| 41627143781 | 2026-11-03T11:23:39Z | 2026-11-03T11:33:24Z | 585 | 440 | 331 |
| 41626212635 | 2026-11-03T11:17:13Z | 2026-11-03T11:27:38Z | 625 | 441 | 355 |
| 41623556421 | 2026-11-03T10:57:38Z | 2026-11-03T11:06:41Z | 543 | 435 | 324 |
| 41621379950 | 2026-11-03T10:41:40Z | 2026-11-03T10:51:41Z | 601 | 467 | 335 |
| 41619877362 | 2026-11-03T10:30:52Z | 2026-11-03T10:40:17Z | 565 | 469 | 364 |

Median critical path: **585 s (9.75 min)**. Maximum individual job duration across
these runs: **469 s** (under the ten-minute per-job ceiling).

Run `40507651605` (push of #970): checkout aggregate failed on main because
`collected` was **12416** while `.verify-floor.json` still held **12388** — floor
drift, not a sharding regression. The existing `reconcile-floor` job on main is the
drain; no code change was required for that failure mode.

### 2026-11-22 acceptance readback

**Pending until 2026-11-22.** Re-measure the five latest successful PR CI runs with
`gh run view <id> --repo stranske/Orchestrator --json jobs` after this repair merges
and has propagated through several PR heads. The median critical path must be at most
six minutes and no single job may exceed ten minutes. This document records pre-repair
hosted measurements only; it does not claim the dated criterion has passed.
