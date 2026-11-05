# Per-attempt receipt locator reconciliation

Checked 2026-11-03 UTC. First incomplete task: reconcile original retries.
The original retry manifest, authenticated native receipts, accepted deployment
binding and current writer ownership remain unavailable. Repository-specific
report recovery code is the edit scope; no synced automation is changed.

Before editing, run once:

```sh
node scripts/run_adjudicator_acceptance.js --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 --revision 408fc9a3cdb169d4d6b3bd65bc58924f52803c8a --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/receipt-binding-followup/acceptance-run
```

Expected: missing pytest blocks collection, named tests and full retrospective
suite. Stop after this runner invocation; each command has a 60-second bound
and skips slow tests. Do not retry package installation.

Then test per-attempt native locator matching using fixtures: match both case
and backend identity, expose duplicates and conflicts, preserve parent/retry
separation, report unavailable journals and absent identities without borrowing
a receipt. Matching is not native authentication or independent truth. Run
existing inventory, runner and ledger regressions, then one read-only inventory
of the previously recovered selected-fields projection. Preserve its hash and
origin separately from the original report. Do not reconstruct the original
manifest from retained rows. No dispatch, publication, production or mirror
write is justified; operational acceptance checkboxes remain open.
