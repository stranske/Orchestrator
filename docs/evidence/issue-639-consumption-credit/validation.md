# Consumption-credit validation — 2026-10-18

Source and regression changes are prepared locally and remain uncommitted.

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

## Blocked gates

Both requested targeted pytest commands, with `-m "not slow"`, failed to start:

```text
No module named pytest
```

Black formatting at line length 100 and the required repository-wide check failed to start:

```text
No module named black
```

An attempted isolated installation failed because the runner could not resolve/reach the package
index. No commit or push was made because the required pre-commit formatting gate cannot pass.
No additional task or acceptance checkbox is claimed complete.

GitHub readback confirmed PR #729 is open and ready for review (`draft=false`). Attempts to post
this blocker and add `needs-human` were rejected by automatic approval review: the connector
requires approval, but this run's approval policy is `never`. Neither mutation succeeded.

Next: provide pytest, Black, and the repository test dependencies; run the targeted regressions
and their actual deliberate-break gate; run Black and its repository-wide check; then commit.
