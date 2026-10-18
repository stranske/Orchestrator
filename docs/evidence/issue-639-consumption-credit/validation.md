# Consumption-credit validation — 2026-10-18

At this historical validation run, source and regression changes were prepared locally and remained uncommitted.

## Available checks

- Production-module imports, Python syntax compilation, and `git diff --check` passed.
- Manual checks exercised the real router, `plan_dispatch`, `_spawn`, lifecycle target,
  and Brain writer, replacing external process launch and provisioning with local fixtures.
- Selection without a compiled binding produced exactly zero accepted capability edges,
  preserved the routing decision, and persisted `target_binding_missing`.
- A supported bounded workflow produced exactly one version-exact accepted edge; its source
  completion event retained the consumer receipt, and dispatch retained both exercised IDs.
- Refused, failed, expired, unsupported, changed-version, unconsumed, and missing-receipt cases
  each produced zero selected-capability edges while baseline dispatch and independent
  infrastructure attribution continued.

The manual router mutation check captured these outcomes:

```text
Disconnected router fix: exit=1
assert assignment['capability_ids'] == [], assignment['capability_ids']
AssertionError: ['selection-credit']

Restored router fix: exit=0
selection through router/plan_dispatch/_spawn/Brain: passed; accepted capability edges = 0
```

The router was restored afterward. This manual check does **not** fulfill the required
deliberate-break gate using the actual pytest regression.

## Gates unavailable in the historical runner

Both requested targeted pytest commands, with `-m "not slow"`, failed to start:

```text
No module named pytest
```

Black formatting at line length 100 and the required repository-wide check failed to start:

```text
No module named black
```

An attempted isolated installation failed because the runner could not resolve/reach the package
index. That validation run made no commit or push because its formatting gate could not run.
No additional task or acceptance checkbox is claimed complete.

GitHub readback confirmed PR #729 is open and ready for review (`draft=false`). Attempts to post
this blocker and add `needs-human` were rejected by automatic approval review: the connector
requires approval, but this run's approval policy is `never`. Neither mutation succeeded.

The local opener recovery below supersedes these historical environment limitations.


## Local opener recovery — 2026-10-18

All three recorded native reservations were inspected by workflow_attempt_id across every head; implementation and commit/push jobs were terminal before the exclusive local pass. Native payloads were fast-forwarded intact, then current main was merged without a conflict. Future keepalive starts were temporarily paused while the local claim is held.

The recovery preserves instrumented role/offload, experiment and supervisor attribution, excludes a selected pair even when its ID names an instrumented built-in, requires a receipt for accepted consumption regardless of producer name, supplies the actual triage-consumption row reference, and persists updated exercised pairs after invocation. Existing synthetic consumption fixtures explicitly name their synthetic receipt. Missing infrastructure versions now fail instead of silently discarding IDs. The missing-binding regression spies on the actual target entrypoint.

Causal evidence in this directory comes from an isolated export built with `scripts/build_exec_mirror.sh`, using the committed source and unchanged regression tests. Restoring selection-as-credit makes the actual router regression fail on `['selection-credit']`; exact restoration passes. Disconnecting both real `_spawn` invocation calls makes the new dispatcher selftest fail because `invoke_compiled_target` is called zero times; exact restoration passes the complete dispatcher selftest. The selected-built-in test also failed before its production guard was added. No working live mirror or Brain was changed by these probes.

Focused selection/lifecycle acceptance: 64 tests passed before the additional selected-built-in case. Existing broad focused run: 404 passed and two new assertion defects were exposed; both were corrected and the full affected selection/lifecycle files then passed. Final focused acceptance and source count are recorded below. Mypy passed for feedback/dispatcher/outcomes; repository-wide Black checked all484 Python files successfully; focused Ruff and diff checks passed.

Full `python3 src/verify.py --floor-may-lag` is running on the pre-final-guard snapshot (4977 collected); no full-suite PASS or hosted/live acceptance is claimed here. Final-head CI, native postmerge verification and original source acceptance remain required.

Final local focused command (causal core, lifecycle, rejected role edges, experiment members, supervisor): **145 passed**. Independently measured final collection: **4978**. Pass floor remains an expectation until final-head full verification; all ceilings are unchanged.

Current main advanced during validation. The serial pass integrated merged #730; only the independently measured collection expectation conflicted, now4991, with every ceiling unchanged. The long full-pytest phase completed:4976tests and319subtests passed; one existing multi-capability regression exposed another omitted instrumented built-in (`adversarial-review`). That original regression is preserved and rerun after restoring its contract. The old verify supervisor was stopped before its later phases; this is not a full verify PASS. Current-head full verification remains required in CI.
