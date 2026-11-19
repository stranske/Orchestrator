# Issue #1324 minimal salvage

Check date: 2026-11-12 UTC. Base: `e14cd229da94be574ec569618b529e10117bddd1`.

Outcome 1: the real checkout passes the issue's focused acceptance command:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_issue1232_ranked_coverage.py tests/test_verify_sharding.py -q --no-cov
```

Before restoration: 25 passed in 25.56s. After restoration: 33 passed in 28.77s.
Stop rule: three failed acceptance attempts or 60 minutes. Deliberate negative
controls are expected failures, not acceptance repair attempts. No full suite is
executed locally; the named sharding test collects the suite to check its partition.

## Comparison with closed PR #1350

Started with `gh pr diff 1350 --repo stranske/Orchestrator`. Current main still
lacks all eight added tests, the argv/subprocess isolation, and the disposable
acceptance dependency manifest. The test module and `requirements-test.txt` are
restored byte-for-byte from #1350 head `ebab510447834730ebab5e060b49b6ccc0d93a30`.
The two old round notes are replaced by this current verification record.

The existing module is already registered exactly once in the shard partition;
there is no new module to register. Production code, dependency pins, exclusions,
skip ceilings and the floor are unchanged. `.verify-floor.json` is byte-identical
to origin/main. PR CI already supports a lagging floor; main's existing reconciler
drains the difference after merge. No local count is set.

Pinned Black 26.10.1 and Ruff 0.17.4 pass for the restored module.
Fresh Python 3.14.3 with pytest 9.2.0 and only `requirements-test.txt` installed
also passes `-m pytest -p no:cov` on the two named modules: 28 passed, five
existing coverage-only skips in 12.16s. This checks disposable collection
dependencies; the literal `--no-cov` command above runs with pytest-cov available.

## Fresh mutation/revert evidence

`mutations.json` records fresh source hashes, commands, expected failure excerpts
and revert-pass results for every one of the eight restored tests. All 17 controls
must have `red: 1` and `restored: 0`. Production bytes are restored after each run.
The 15 prior controls were rerun, plus controls for veto severity and closed-PR
eligibility, so every restored test has current evidence.

The old dispatch record used an ambiguous occurrence index: replaying it as a
zero-based index mutated the unrecorded adjudicate branch and passed. The current
record targets the first call, inside `if args.dispatch`, and fails at the exact
dispatch outcome assertion. This harness correction did not change a test.

## Target coverage and limits

The focused before/after measurement runs only the named ranked-coverage module,
on identical production source, with separate coverage files:

```sh
COVERAGE_FILE=/tmp/issue-1324-baseline.coverage /opt/anaconda3/bin/python3 -m pytest tests/test_issue1232_ranked_coverage.py -q --cov=runtime_ac_panel --cov=relearn_report --cov=keepalive_supervisor --cov-report=json:/tmp/issue-1324-baseline-coverage.json
# Restore #1350's module, then use the same command with restored output paths.
```

| Target | Main's three tests | Restored eleven tests | Gain |
| --- | ---: | ---: | ---: |
| runtime_ac_panel | 158/338 | 246/338 | +88 |
| relearn_report | 126/260 | 150/260 | +24 |
| keepalive_supervisor | 169/659 | 179/659 | +10 |
| Total | 453/1257 | 575/1257 | +122 |

This isolates the tests' contribution; it is not combined Gate coverage. Each
target retains two excluded lines, and branch coverage is unmeasured.

The salvaged combined Gate evidence is available at commit
`db26b38a44c92995dea50249277e2ed0c4d5b302`,
`docs/verification/issue-1324-round7/gate-targets.json`:

| Target | #1233 CI 42431925653 | Later CI 42863205636 | Gain |
| --- | ---: | ---: | ---: |
| runtime_ac_panel | 289/338 | 337/338 | +48 |
| relearn_report | 243/260 | 260/260 | +17 |
| keepalive_supervisor | 615/659 | 632/659 | +17 |
| Total | 1147/1257 | 1229/1257 | +82 |

These historical runs establish +82 statements, 98 below the original approximate
180-unit estimate; they do not establish current-head CI or attribute all gains
to this change. Current panel/supervisor source differs due to the aider retirement;
relearn source is unchanged. The resulting PR's current-head `gate-coverage`
artifact must establish the fresh combined result. Normal and flat exec-mirror
CI results and artifact links belong in the PR body after readback.

## Follow-up scope

- Panel: the historical one-line fallback follows argparse's required action
  group. Keep it documented rather than weakening the parser or excluding it.
- Relearning: historical statement coverage is saturated; branch coverage remains
  unknown and needs a separate measurement.
- Supervisor: the historical 27 missing statements include the backend extension
  now exercised here. Remaining closure validation/locking/budget, Stage-2
  candidate/error/replay, corpus filtering and heartbeat failure scenarios need
  separate hermetic coverage. Fresh CI may change the exact count.
- The 122-statement focused gain leaves 58 of the approximate 180-unit goal
  unmeasured by this isolated comparison. It cannot be added to the historical
  combined gain, since they overlap. No claim of full coverage completion is made.
