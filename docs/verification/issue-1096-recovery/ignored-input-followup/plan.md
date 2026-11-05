# Ignored acceptance inputs, 2026-11-03 UTC

Recorded before the required pytest attempt. The first unchecked task remains
the four named tests and full retrospective suite. Source commit
`a4e8c0c800839088b63df4b8cff5935c689b8d7b` has checkout parent
`1ae4e735d996af6069a943f7958cb1d8c93412da`. Neither is verified as the accepted
production deployment. Checkout Git metadata is read-only; the source commit
is preserved in `/tmp/issue-1096-ignored-git-kKjd0F`.

```sh
GIT_DIR=/tmp/issue-1096-ignored-git-kKjd0F \
GIT_WORK_TREE=/home/runner/work/Orchestrator/Orchestrator \
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 \
  --revision a4e8c0c800839088b63df4b8cff5935c689b8d7b \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/ignored-input-followup/acceptance-run
```

Expected: missing pytest prevents collection and both test invocations; preserve
the exact commands and failures with `required_tests_passed=false`. Dependency
installation was attempted this round but no pytest distribution was available.
Stop after this runner invocation. Each command has a 60-second timeout, uses
`-m "not slow"`, and runs with a temporary state directory. Do not dispatch,
publish, synchronize a mirror, or modify production state.

The source fix rejects ignored source and fixture inputs during both source
checks. Its controls distinguish those inputs from ordinary Python and pytest
caches. Five new negative controls failed before the fix; the final runner
controls pass 33/33, inventory controls 53/53, and ledger controls 22/22. These
test-double controls do not substitute for running the actual Python suite.

Recent commits and existing receipts were reviewed before implementation.
Only the three previously completed distinct deliverables remain complete;
duplicate checkboxes require reconciliation as recorded in
`task-reconciliation.md`. The connector rejected the remote PR-body update
because it requires approval while the current policy is `never`.

The original retry and fixed next-20 manifests, accepted deployment binding,
authenticated native receipts, current canonical report, and writer ownership
remain unavailable. Partial recovery is explicitly left unchecked; no acceptance
checkboxes are completed by this followup.
