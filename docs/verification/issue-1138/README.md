# Issue 1138 continuation acceptance

Checked 2026-11-04 America/Chicago (2026-11-05 UTC).

Outcome: all 34 existing tests must fail on a targeted production break and pass after
restoration; the new module must be assigned once; updated-head CI must pass. Stop rule:
three unsuccessful attempts at one step or 60 minutes, report the first broken step.

Local real-input commands (repository-selected venv absent):

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_verify_sharding.py::test_shard_lists_partition_every_collected_test_exactly_once
/opt/anaconda3/bin/python3 -m pytest -q tests/test_coverage_round_1138.py
```

The first printed `1 passed in 24.23s`. The initial module run exposed three isolation
failures: cached agent runtime setup and live handoff claim reaping escaped the environment
fixture. The fixture now pins HANDOFF_DIR, disables capability heartbeats, and stubs the two
runtime/auth prelude builders in the command-construction test. The subprocess/socket guards
and both temporary state/runtime settings remain. After repair: `34 passed in 0.49s`.

The verdict test queries the actual verification completion event and asserts exact node IDs,
excluding the file-only fallback. No production changes are retained.

## Per-test deliberate-break evidence

[mutation-proof.json](mutation-proof.json) records all 34 individual node commands, exact
production function and before/broken replacement, complete failing output (exit 1), and
restored passing output (exit 0). Each mutation was applied to production source, compiled,
run against the single test node, and restored in a finally block before the success run.
All 34 mutations failed their intended tests; no selected mutation survived. The original
branch's one recorded experiment remains in PR history; this adds proofs for every node.

## Combined coverage and provenance

[base-combined.json](base-combined.json) retains the six summaries from the exact base commit
555c1f1cf0ac8a6fb3e64d4081ecf952df126021, CI run 41952625547, artifact 13382156456.
All six coverage shards succeeded. The aggregate published 141 instrumented processes;
its failure was a seven-test collection-floor lag, not failing tests. The overall run was
cancelled, so this is a measured baseline, not a claim of green base CI.

The updated PR's CI runs the same combined producer without running the full suite locally:

```sh
python3 src/verify.py --pytest-shard N/6 --coverage --shard-output shard-results
python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag
```

CI uses Python 3.14 and VERIFY_PYTEST_WORKERS=4. Combined coverage includes pytest and
module selftests. The configuration has branch_coverage=false: missing units here are
statements, not measured branches. Exact updated-head deltas and shortfall are recorded in
the PR body after CI publishes its artifact. Record all six reductions, their combined
delta and the shortfall from 180 before closing the issue. If the artifact is missing,
record UNKNOWN and keep the issue open until coverage progress is measured.

Target order is inherited from issue #1138's Orchestrator ranking (escaped-defect priority,
then churn, then uncovered mass), not independently reranked or chosen by file size. The
base artifact exactly reproduces its six advertised gaps: 10, 46, 9, 69, 23, 160 (317 total).
The source ranking scores and escaped-defect/churn inputs were not supplied in the issue;
this PR does not claim to have recomputed them.

Target limitations: local_verify's remaining paths include git extraction and verification
failures outside these two helper tests. capability_activation_audit tests focus on error
recovery and diagnostics, not every external workflow/caller path. exp_abcd's nine baseline
missing statements are all in _selftest_checks, so evaluator tests can improve regression
protection without reducing that gap. periodic_report still has substantial unexercised
CLI/build-report/formatting paths. tick's research test stops on a lost claim, so later
experiment-launch and metadata paths are not covered by it. repo_knowledge has broad
registry migration, export, suggestion, retrieval and CLI paths beyond its four tests.
These paths are not asserted to be dead or impossible to fake; this is partial delivery.
Floors, skip ceilings, dependency pins and production behavior are unchanged.

Post-push review strengthened the caller fallback to prove a positive exact caller and a
negative nonmatching function, and the malformed metadata test to require empty arm and
missing-outcome lists. Both changed tests received fresh targeted mutation proofs.
