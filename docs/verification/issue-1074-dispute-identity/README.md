# Issue 1074: dispute identity reconciliation

- [ ] Establish a read-only inventory of the requested 71 disputes.

Reviewed commits `d470db0` through `e28da4d` and their inventory, retrospective,
closer and named-test receipts before this change. The original Brain and
historical manifest remain unavailable. Existing fixtures and historical receipts
do not establish operational acceptance. No new broad checkbox is completed.

The inventory exporter previously reconciled only run ID and target. Production
`adjudicator_retro._case_identity` identifies a dispute by run ID and both
verdicts. Consequently an existing run with changed verdicts could satisfy the
older manifest despite representing a different dispute.

The exporter now accepts a version 2 manifest:

```json
{
  "schema_version": 2,
  "cases": [
    {
      "run_id": "original-run-id",
      "target": "owner/repo#123",
      "verifier_verdict": "NON_PASS",
      "adjudicated_verdict": "PASS"
    }
  ]
}
```

Both verdict strings are required and compared exactly. Changed verdicts produce
an identity mismatch, a requested-case evidence gap, and an unexpected observed
dispute, even when run IDs, targets and counts match. Different disputes on one
run remain distinct requested entries; duplicate dispute identities are rejected.
Excluded, missing and unavailable entries retain their full requested identities.
Version 1 manifests remain supported. The reconciliation's `identity_scope`
explicitly distinguishes `run_target` from `dispute`. Neither format independently
establishes historical provenance: `requested_cohort_verified` stays false.

All 23 inventory tests pass, including seven new version 2 tests. The synthetic
retrospective batch (1), closer tests (9), receipt tests (9), Gate-gap tests (4),
evidence unittests (11) and retrospective selftest (23 checks) also pass. Syntax
and whitespace checks pass. No Python files changed. Current named pytest and
the full verifier were not run; pytest is unavailable in this runner.

An isolated mutation accepting every verdict pair fails four new tests. Exact
restoration passes all seven version 2 tests. [validation.json](validation.json)
binds the source, tests, mutation and logs. This is a synthetic identity control,
not either required named acceptance mutation or an operational replay receipt.

The PR-body reconciliation was attempted before implementation: both copies
of verified inspection, Gate and historical receipt tasks/criteria are checked
in the prepared body, and `Closes #1074` becomes `Tracks #1074`. Automatic approval
review rejected the update because connector mutations require approval while
the run policy is `never`. The prepared body is saved under
`/tmp/1074-dispute-identity-delivery/pr-body.md`; remote metadata remains unchanged.

The blocker comment and `needs-human` label were also rejected for the same
approval restriction. Staging failed because `.git/index.lock` is on a read-only
filesystem. A commit and transfer patch are prepared with separate Git metadata
under `/tmp/1074-dispute-identity-delivery`; the workspace edits remain available
for automation. PR #1075 was verified open and ready for review.
