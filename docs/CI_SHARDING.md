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
python3 tools/make_test_shards.py junit.xml --shards 6
python3 -m pytest -q tests/test_verify_sharding.py
```

The generator balances modules by summed JUnit timing, with deterministic ties.
It lists modules absent from the timing sample and assigns them the median measured
duration. The initial plan used the 2026-10-25 local control; the current plan
uses hosted run 40422953711. The newly merged
`tests/test_closer_coding_session_v2.py` was measured locally at 0.41 test seconds
and assigned to shard 5 as untimed input until the next hosted JUnit refresh.
JUnit is used here only for balancing timings. Hosted job minutes and pipeline
wall time are read separately from `gh run view --json jobs`.

Acceptance remains the issue's dated measurement: on 2026-11-22, inspect the five
latest successful PR CI runs using `gh run view <id> --json jobs`. Measure from the
earliest shard start to the latest aggregate completion. The median must be at most
six minutes and no individual job may exceed ten minutes. Include the flat variant;
a single fast run does not establish that median.
