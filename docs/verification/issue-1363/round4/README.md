# Retained archive rotation regressions

Reconciliation reviewed `bffb40ef`, `e7010e4b`, and `790efac1`. Those commits
added runner and ledger-observation tests and runner-local evidence. The first
three task boxes are already checked on PR #1365; no additional task or
acceptance criterion has been verified complete. The connector rejected the
reconciliation body update and `needs-human` label with `MCP tool call requires
approval, but approval policy is never`.

The first unchecked task is blocked by missing pytest. The selected interpreter
is `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`. `verify-context.json`
records its version, checkout SHA, `origin/main` SHA, exact command, and temporary
runtime overrides. `verify.log` and `verify.exit` retain the full verifier result.
It exits 1: zero pytest tests collected (floor 8636), 105 of 111 selftest modules
passed, five reported prerequisite skips, and `local_verify` failed. Capability
admission fails on missing pytest; the activation audit passes. No reported
deployment heartbeat defect was reproduced here.
The run uses `PYTEST_ADDOPTS='-m "not slow"'` and the existing writable runtime
and handoff overrides. The floor comparison in `floor-comparison.log` exits zero;
the floor is unchanged. `prerequisites.log` reproduces missing pytest in the
provision suite and capability admission. `local-verify-reproduction.log` records
the standalone local-verifier selftest failing because pytest is not importable.
`dependency-install.log` retains the unsuccessful installation attempt. Black is
also unavailable; no repository Python files were edited.

While deployment is blocked, two JavaScript integration cases advance the next
task's retained-ledger reporting checks. They run the real collector over
disposable inputs with two identical segments holding refusals and successful
retries in both lanes. After rotating both segments, they verify that an old
receipt cannot qualify the changed layout and then carry the collector's state
into a fresh observation. That observation preserves the original continuous
baseline and counts all four retained refusals, or six when identical events
also appear in the new active segment. The CLI exits 1 with continuous coverage
and unmet acceptance, and receipt and ledger bytes remain unchanged. These are
test observations, not deployment receipts.

Validation: all 43 ledger-auditor tests and all 39 native-witness runner tests
pass with an explicitly selected Python. Complete outputs and exit statuses are
retained here. Native-runner tests simulate pytest and do not establish native
deployment acceptance. `rotation-mutations.log` shows both new cases detecting
disposable mutations that ignore archives or reject intact renamed prefixes.
Node syntax and `git diff --check` pass.

`deployment-paths.log` records that the runtime and installed mirror paths are
absent here. No production collector was invoked, archive removed, deployment
performed, or seven-day acceptance claimed. The delivered checkout and generation
still need the full verifier, native suites, and a fresh existing hourly collector
observation on the deployment host.

`checks-and-staging.log` records `git add` failing because `.git` is mounted
read-only and Git cannot create `index.lock`. No commit could be created; the
test change and evidence remain in the working tree for the automation owner.
The blocker-comment request was also rejected by the connector's approval
requirement. Final readback confirms PR #1365 is open with `isDraft=false` at
`790efac1cbf37f659c9e9dab8036c9fb29170369`.
