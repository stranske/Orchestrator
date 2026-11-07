# Issue 1154 closer repair acceptance

Check date: 2026-11-05.

Outcome 1: all **76** collected pytest node IDs (both new modules) reject one named,
behavior-specific temporary production mutation with the intended failure at the mapped test
line, then pass individually after byte-for-byte restoration. **Observed: 76/76** on
2026-11-05 (`611.223s` total pair time; receipts in `tests/evidence_1154/results.json`).
Production has no committed diff for coverage targets.

Commands (repository Python `/opt/anaconda3/bin/python3`; no selected venv in this worktree):

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue1154_ranked_coverage.py tests/test_issue1154_tick_boundaries.py
/opt/anaconda3/bin/python3 tests/mutation_runner_1154.py --output tests/evidence_1154/results.json
/opt/anaconda3/bin/python3 -m pytest -q tests/test_native_storage_tmpdir.py
```

Stop rule: two attempts per failing command or 30 minutes before reporting the first
unresolved step; mutation batch stops on first pair failure (55s per pytest process, 120s
per pair).

## Branch reconciliation

- `closer-r1/issue-1154`: no commits ahead of `origin/main`; nothing merged or discarded.
- `closer-r2/issue-1154`: completion work on current main.

## Merge-commit CI failure (`115395f0458012d5adf32f0b0c46a7458559666c`)

Failed workflow: [CI run 42082525455](https://github.com/stranske/Orchestrator/actions/runs/42082525455)
(push to `main` after #1159 merged).

Failed job: **verify.py aggregate + combined coverage** — collection floor on `main` requires
equality with measured collection. The merge added **76** tests; the floor still read **14465**
while pytest collected **14541** (`76 BEHIND`). Tests passed; verify exited 1 on the floor
check only.

**Attribution:** Expected lag from test-only delivery without a floor edit (issue #1154 and
repo policy forbid editing `.verify-floor.json` in the opener PR). **Not** a regression in the
new tests.

**Drain:** `c259e492` (`chore(floor): reconcile the collection floor on main`) on current
main. No production repair required for this completion PR.

## Combined coverage (Gate artifact, statements and branches separate)

Authority: CI **gate-coverage** artifact from PR #1159 CI
([run 42077325264](https://github.com/stranske/Orchestrator/actions/runs/42077325264)), same
combined measure as `verify.py --coverage`.

**Baseline drift:** Issue body ranked gaps from `main @ b69ff474 (2026-11-05T06:45:30Z)` (~267
combined statement+branch units across the five files). `capability_recurrence_check.py` and
`capability_propensity.py` line lists were already stale when #1159 opened (“no line list — PR
merged since that CI run”). Current main moved again after #1159; counts below are from the
#1159 Gate artifact, not a re-run of `verify.py --coverage` locally (forbidden full-tree load
on this machine during closer repair).

| Module | Baseline (issue, stmt+branch) | After #1159 (stmt) | After #1159 (branch) | Δ (stmt+branch) |
| --- | ---: | ---: | ---: | ---: |
| `src/capability_recurrence_check.py` | 15 | 15 | 0 | 0 |
| `src/capability_propensity.py` | 20 | 20 | 0 | 0 |
| `src/partitioned_review.py` | 113 | 19 | 0 | 94 |
| `src/mcp_server.py` | 9 | 1 | 0 | 8 |
| `src/capability_outcome_bridge.py` | 110 | 28 | 0 | 82 |
| **Total** | **267** | **83** | **0** | **184** |

**Partial delivery (explicit):** Goal was ~180 units; measured **184** on statements (branches
unchanged at 0 in this artifact). **`capability_recurrence_check.py` and
`capability_propensity.py` did not move** in combined coverage despite new hermetic tests:
those lines are already exercised by module `--selftest` and existing pytest; the new modules
add regression discrimination, not Gate-visible statement mass on those two files. Remaining
83 statement gaps are mostly deep bridge paths and recurrence/propensity corners not reachable
without widening scope beyond #1159’s test-only contract.

## Native storage integration test (`tests/test_native_storage_tmpdir.py`)

**Process-stubbing conflict:** PR #1159 briefly imported `controlled_world` / `native_world`
from `test_closer_reasoning_boundary.py`. That module pulls closer finite-reasoning fixtures
that patch `subprocess`, `Popen`, and dispatch surfaces for hermetic offline tests. The
**actual-supervisor** case (`test_actual_supervisor_retires_native_tmp_on_success_and_failure`)
must run real `subprocess.run` / bash / `storage.run_job` — stubbing would mock away the
behavior under test.

**Resolution:** Remove the unused closer-boundary imports (they were never referenced). Keep
the supervisor synchronization fix from #1159 (ready-file gate on `Reservations.bind`,
`|| exit "$?"` on the injected child, isolated `ORCH_STATE_DIR` / `ORCH_LOCAL_RUNTIME`) as the
documented integration correction — not replaced by mocks.

## Mutation evidence

- Case map: [`tests/mutation_cases_1154.json`](../tests/mutation_cases_1154.json)
- Runner: [`tests/mutation_runner_1154.py`](../tests/mutation_runner_1154.py)
- Receipts: [`tests/evidence_1154/results.json`](../tests/evidence_1154/results.json)
- Narrative + focused pytest output: [`tests/mutation_evidence_1154.md`](../tests/mutation_evidence_1154.md)

## Focused pytest (2026-11-05)

See [`tests/evidence_1154/focused.txt`](../tests/evidence_1154/focused.txt):

```text
76 passed in 0.68s
```
