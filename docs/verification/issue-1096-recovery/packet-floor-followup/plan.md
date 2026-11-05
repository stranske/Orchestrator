# Packet evidence floor diagnostics, 2026-11-03 UTC

The first incomplete task remains original retry reconciliation. This change
adds read-only packet diagnostics to the repository-specific inventory; it does
not change the Python evidence floor or dispatch/publication behavior.

Before source changes, run once:

```sh
node scripts/run_adjudicator_acceptance.js --python /opt/hostedtoolcache/Python/3.14.8/x64/bin/python3 --revision 8bef6a28fce7b4eecbaa3fc08016d7bb505742b5 --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/packet-floor-followup/acceptance-run
```

Expected: pytest is unavailable before collection. Preserve command output with
the revision and interpreter. Stop after this invocation, with the runner's
60-second per-command bounds and `-m "not slow"`; no installation retries.

Add regressions before implementation for missing/truncated diff evidence,
missing or malformed criteria, serialized qualification, misleading flags and
separate parent/retry packets. Compare classifications against the existing
Python `_row_metadata_only` function over well-formed JSON packet shapes. Keep
malformed packets explicitly unavailable. Bind observations by packet JSON hash
and location, without exposing packet text. Run inventory, acceptance runner and
ledger tests. Use only a recovered input or explicitly identified diagnostic
projection for any historical exercise; never reconstruct original membership.

The original requested 21-row manifest and authenticated native journal are not
available. Exact-name external searches did not recover the referenced original
results file. Independent current truth, accepted/deployed/mirror bindings and
live writer ownership remain unverified. No paid retry or canonical regrade is
authorized by these diagnostic observations; no task checkbox is completed.
