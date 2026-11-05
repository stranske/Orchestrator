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
statements, not measured branches.

Local intersection against the base artifact's missing statement lists, after the second
test pass (67 tests in `tests/test_coverage_round_1138.py`): the new tests execute 248 of
the 317 baseline-missing statements. That is the predicted CI reduction for these lines,
because the base artifact already includes the full suite plus selftests and these lines
were still missing there. It is not a substitute for the updated-head Gate artifact.

| Target | Base missing | Executed by this module | Still missing here |
|---|---:|---:|---:|
| local_verify | 10 | 10 | 0 |
| capability_activation_audit | 46 | 42 | 4 |
| exp_abcd | 9 | 0 | 9 |
| periodic_report | 69 | 63 | 6 |
| tick | 23 | 20 | 3 |
| repo_knowledge | 160 | 113 | 47 |
| total | 317 | 248 | 69 |

Predicted shortfall from 180: 0 (248 >= 180). Official head deltas stay pending until the
updated-head coverage artifact is published. The 69 statements this module still does not
execute are selftest-only branches (`exp_abcd._selftest_checks` and its nested fakes,
`capability_activation_audit._selftest`, `periodic_report._selftest`, `tick._selftest`),
`periodic_report.build_report` corrupt-artifact handlers, and `repo_knowledge` git-remote,
suggestion-import, and feedback-memory paths.

Target order is inherited from issue #1138's Orchestrator ranking (escaped-defect priority,
then churn, then uncovered mass), not independently reranked or chosen by file size. The
base artifact exactly reproduces its six advertised gaps: 10, 46, 9, 69, 23, 160 (317 total).
The source ranking scores and escaped-defect/churn inputs were not supplied in the issue;
this PR does not claim to have recomputed them.

The 69 statements still missing from this module are not claimed to be dead. `local_verify`'s
ten baseline statements are all executed here. Floors, skip ceilings, dependency pins and
production behavior are unchanged.

Post-push review strengthened the caller fallback to prove a positive exact caller and a
negative nonmatching function, and the malformed metadata test to require empty arm and
missing-outcome lists. Both changed tests received fresh targeted mutation proofs.

The second pass adds 33 tests. [mutation-proof-pass2.json](mutation-proof-pass2.json)
records each one's broken exit 1 and restored exit 0. One first mutation of the snapshot
CLI test survived because argparse usage still printed `--from-gap`; the assertion now
requires the exact `parser.error` sentence, and that mutation was repeated and killed.

## Third pass

Ten more tests cover the production paths the second pass still missed:
`periodic_report.build_report` corrupt pattern-miner artifacts, and `repo_knowledge`
fuzzy playbook duplicates, git-remote parsing, doc and review suggestion filters,
PR-comment payload failures, snapshot skips, tokenless memory scores, and feedback-memory
query failures. Each failed under a real production replacement and passed after
restoration ([mutation-proof-pass3.json](mutation-proof-pass3.json)). No production
change remains.

Local intersection against the same base artifact, 77 tests in this module:

| Target | Base missing | Executed by this module | Still missing here |
|---|---:|---:|---:|
| local_verify | 10 | 10 | 0 |
| capability_activation_audit | 46 | 42 | 4 |
| exp_abcd | 9 | 0 | 9 |
| periodic_report | 69 | 67 | 2 |
| tick | 23 | 20 | 3 |
| repo_knowledge | 160 | 157 | 3 |
| total | 317 | 296 | 21 |

Predicted shortfall from 180: 0 (296 >= 180). This is the same intersection method that
matched the `88df4821` artifact's 88/88 gain. It is still not the Gate artifact for this
head.

The 21 statements this module does not execute are inside `_selftest` bodies, and the
combined baseline already runs those selftests without reaching them:

- `exp_abcd.py` 3158, 3170, 3179, 3301, 3473, 3548, 3618, 3772, 3774: environment-restore
  `else` branches taken only when the variable was already set, nested fake-completion
  and fake-`Popen.kill` helpers, and assertion-failure raises.
- `capability_activation_audit.py` 2432-2433, 2436, 2448: the live-ledger
  `docs-drift-fix-agent` check and the `ORCH_FLEET_ROOT`-already-set restore. CI
  bootstraps an empty ledger and does not set that variable.
- `periodic_report.py` 2998, 3119: the selftest's own `AssertionError` and
  verdict-consistency assert, taken only when the selftest's expectation fails.
- `tick.py` 897, 1289, 1293: restores of `ORCH_DISPATCH_LANE`, `ORCH_EXPLORATION_RATE`,
  and `HANDOFF_DIR` when those variables were already set. The unset branches are the
  ones the selftest runs.
- `repo_knowledge.py` 1994-1995: loop body over `provision.BASE_BRANCH_OVERRIDES`, which
  is an empty dict, so the body cannot run. Line 2490 is the selftest's
  `AssertionError` when a stubbed `gh` failure is not surfaced; the production
  `RuntimeError` is covered by `test_suggest_from_pr_comments_reports_bad_targets_and_payloads`.

Running those `_selftest` functions from pytest would re-enter non-hermetic setup
(real ledger lookups, nested subprocess fakes, and assertion failures of the selftest
itself). They are not a separate production behavior to score.
