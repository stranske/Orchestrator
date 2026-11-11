# Round 9 acceptance record — PR #1341

Check date: 2026-11-11. Continuation on the existing `orch/issue-1324` branch.

Outcome: the repository-selected interpreter must print a passing result for:

```sh
.venv/bin/python -m pytest tests/test_issue1232_ranked_coverage.py tests/test_verify_sharding.py -q --no-cov
```

Continuation stop rule, recorded before editing: two repair attempts per failing step or
30 minutes. Baseline: **33 passed in 6.24s**. Final changed-tree acceptance:
**33 passed in 12.84s**; actual output is retained in `acceptance.txt`.
There are 11 ranked-coverage tests and 22 sharding tests.

## Assertions

- Equal-score input `alpha,zebra` and prior order `zebra,alpha` prove that prior rank
  overrides both input and alphabetical order. The separate alphabetical fallback remains.
- Eligible planning asserts `operator_inspect`, `eligible=True`, empty blockers, report
  existence, and the exact suffix `--report-json`, report path, `--ac`, `DEFAULT_AC`, `--dispatch`.
- A named `ready_priors` dictionary verifies task1/agent2 becomes approximately **0.675**;
  the comparison count remains exactly 10.
- Previously completed isolated recording and CLI tests remain intact. Black formatted the
  entire named file as requested, without changing their behavior.

## Executed mutation audit

The previous inventory claimed 10 cases but contained 11 records. This corrected inventory
contains **14 executed cases**, each with exit **1** mutated and **0** restored, and
**28 retained transcripts**. `mutations.json` records the exact executed node ID, command,
production replacement, source line, original SHA-256, actual pytest failure lines, and
red/restored transcript paths. An `occurrence` field selects the one-based replacement
when a fragment occurs more than once; otherwise the first occurrence is used.
Each production file was restored in `finally`, checked byte-for-byte before its passing
run, and matches the starting commit after all mutations. No production edit ships.

| Case | Actual discrimination |
| --- | --- |
| 01 | Exact recorded outcome tuple rejects a wrong notes prefix |
| 02 | First ordered gap tuple rejects a wrong reviewer prefix |
| 03 | Exact citation names reject an empty list |
| 04 | Dispatch-only recording removal fails the dispatch outcome count; earlier recording phases pass |
| 05 | Removing malformed-review validation produces AttributeError inside the expected-ValueError context |
| 06 | Removing the prior-rank key fails the `zebra,alpha` assertion |
| 07 | Removing the agent-name key passes prior ordering and fails the alphabetical fallback assertion |
| 08 | Wrong operator next step fails its exact assertion |
| 09 | False returned eligibility fails `plan["eligible"] is True` |
| 10 | Injected returned blocker fails the exact empty-blockers assertion |
| 11 | Wrong backend flag fails the dedicated backend test's exact suffix assertion |
| 12 | Wrong AC argument fails the eligible plan's exact five-element suffix assertion |
| 13 | Changed BT prior coefficient fails task1/agent2 approximately 0.675 |
| 14 | plan_target forwards exact target to gather_signals; wrong target fails forwarding assertion |

This replaces unsupported claims, including the old dispatch mutation that failed an
 earlier phase, prior-order mutation that actually changed score ordering, alphabetical
mutation that left alphabetical ordering intact, and backend mutation assigned to a test
that never enabled the backend. Case 05 is an exception-type failure, not a fabricated
assertion mismatch. Full pytest output supplies context for every summarized failure.

## Formatting and environment

Requested commands:

```sh
.venv/bin/python -m black --line-length 100 tests/test_issue1232_ranked_coverage.py
.venv/bin/python -m black --line-length 100 --check tests/test_issue1232_ranked_coverage.py
```

They printed `1 file reformatted` and `1 file would be left unchanged`, respectively.
The final check output is retained in `black-check.txt`. Black was absent from the disposable
venv and was installed there; no dependency declaration or pin changed.

## Boundaries and retained work

`git worktree list --porcelain` showed this as the sole attached worktree for the PR branch.
The preserved `preserved/stranske__Orchestrator__1324__opener-20261110T161523Z` ref was inspected;
its useful test groups are retained and strengthened on this continuation. No holder was
reset or cleaned. Round 7 mutation and quantified combined statement-coverage evidence,
and round 8 dependency/collection diagnostics remain unchanged.

No full local suite ran. No scope, collection floor, coverage floor, skip ceiling, shard
manifest, exclusion, production behavior, or dependency pin changed in this continuation.
Final pushed-head normal/flat CI, Gate, and review dispositions are recorded on PR #1334;
local focused acceptance alone does not establish those remote results.
