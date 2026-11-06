# Retained retry identity validation, 2026-11-04 UTC

The first incomplete task is original retry reconciliation. The inventory currently
attributes nested attempts to the root case without comparing explicit saved
case/run/target/verdict identities. A contradictory retry can therefore acquire
the parent's manifest and native receipt locators. Add refusal tests before fixing
that attribution. Omitted identity fields remain supported for compact histories;
explicit fields must match the root and must not introduce an unverifiable identity.
Keep original bytes, paid history and independent truth untouched. Refuse the CLI
without partial JSON, and omit conflicting values from errors.

Before implementation, run the existing required-test runner once:

```sh
node scripts/run_adjudicator_acceptance.js --python /opt/hostedtoolcache/Python/3.14.10/x64/bin/python3 --revision ba918d748be8dc58d50741734a2930a10a20ad69 --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/retry-identity-followup/acceptance-run
```

Expected: missing pytest before collection; this is checkout evidence, not accepted
deployment evidence. Stop after this invocation, with the runner's 60-second
per-command bounds and `-m "not slow"`. Run inventory, runner and ledger regressions
directly under Node, preserving counts and source/log hashes. No Python edits.

Read-only GitHub #579/#1072/#1096 and Dropbox searches did not recover the original
retry manifest, row-level results, authenticated journal or fixed next-20 manifest.
Search hits were references and source snapshots. Historical #579 comments report
retry outcomes but cannot replace the requested original rows or native receipts.
Accepted/deployed/mirror bindings and current live writer ownership remain
unverified. Do not dispatch or publish; no operational task checkbox is completed.
