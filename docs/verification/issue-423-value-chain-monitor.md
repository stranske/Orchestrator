# Issue 423: independent capability demand and first breaks

Source implementation for #423, integrated with main `3fbac9b5124057f01ccb36e7214cef2824583364`.
Weekly switch review now renders every live declared capability, separately counting
independent demand, offers, production invocations, successes, accepted influence
and graded durable outcomes. Missing populations remain unmeasured. Historical
advisor events without a measured precondition are not backfilled. The monitor
reuses the ledger and Brain; it cannot open gates or enable the dispatch lane.

## Acceptance evidence

- `python3 src/verify.py`, with a private ledger containing the new declaration and
  a private Brain snapshot: **1900 collected and passed, zero skips; 100/100
  selftests; activation, recurrence, set coverage, admission and ledger validation
  all green**. The admission gate examined all 49 declared rows. Existing skip
  ceilings and mypy exemption ceilings are unchanged.
- `python3 -m mypy --follow-imports=silent src/*.py`: 117 source files, no issues.
- `black --check --line-length 100 --exclude '(\.venv|\.workflows-lib|node_modules)' .`: 322 files unchanged.
- `python3 -m ruff check src tests` and `git diff --check`: clean.
- All four issue-named pytest nodes ran in the suite. The new module has fifteen
  tests, including complete fleet pagination, older open issues, inaccessible
  inputs, fixture/trial exclusion, kill-switch collection bypass, and independent
  precondition persistence.

Two deliberate regressions were applied to `src/value_chain_monitor.py` one at a
time and restored byte-for-byte:

| Fault | Exact pytest node | Broken / restored exit |
| --- | --- | --- |
| Coerce missing situation count to zero | `tests/test_value_chain_monitor.py::test_unmeasured_demand_never_prints_as_zero` | 1 / 0 |
| Report works when outcome is zero | `tests/test_value_chain_monitor.py::test_first_break_is_the_earliest_failed_step` | 1 / 0 |

The supplemental `local_verify.py` comparison to main was red at collection
because main lacks the new module. Its per-node attribution was unavailable;
that comparison does **not** prove all twelve original nodes discriminate the base. The
two actual code mutations above provide the requested acceptance controls.

## Read-only population check

A complete 90-day creation population plus current open issues across sixteen
supported repositories yielded 812 issues filed in hour batches of at least
three, two open issues with at least twelve tasks, and 82 current open issues.
The snapshot reported 47 live rows, zero input errors, and explicitly displayed
`input_off:ORCH_DISPATCH_LANE` when supplied the disabled dispatch input. These
are time-bounded source-checkout measurements, not deployed weekly acceptance.
Unrecorded lane probe or cadence populations remain unmeasured.

## Registration and handoff

The production weekly caller registers the declaration and emits a heartbeat;
`ORCH_VALUE_CHAIN_MONITOR=0` bypasses collection/reporting. Binding limits remain
unchanged: feature scan shares `rail-exercise:brain` with feature reflection,
while evidence acquisition keeps its `tick:learning` binding. The new monitor
is bound to bare `tick` and `rail-exercise:audit`.

No live mirror, wrappers, flags, automation prompts or schedules were published.
Installed weekly acceptance remains a separate manual-publication and closer
step under CLAUDE.md section 1. Keepalive owns subsequent CI/review repairs;
closer owns complete expected-check topology, seven-minute review floor, merge
and post-merge verification.

Raw logs, the two RED/GREEN controls, read-only report and private-state verifier
receipt are retained at
`/Users/teacher/.codex/automations/pd-workloop-resume/evidence/20261005T0701Z/`.

## Review recovery on the integrated candidate

Both full CodeRabbit threads4181825219/4181825241 were checked. Registration,
collection and report exceptions now preserve the weekly artifact with visible
errors; the failure fallback remains enabled. Observed direct invocations bypass
the advisor-offer step while preserving the recorded zero offer count. Three
new regression nodes failed before the fix and passed after it; both original
mutation controls were repeated on this final source and reject their faults.
The full1900-test private verifier and existing ceilings pass after integrating
dispatcher461; subsequent remote CI and installed acceptance remain separate.
