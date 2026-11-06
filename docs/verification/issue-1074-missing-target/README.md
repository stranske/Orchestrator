# Issue 1074: unavailable target evidence

The inventory exporter now records a per-run `missing_target` evidence gap when
an existing run has a null, empty or whitespace-only target. Supplied manifests
of either version classify that requested identity as `unavailable`, preserving
other exclusion reasons. A nonblank contradictory target still produces an
identity mismatch. Inventory eligibility remains identical to the production
retrospective reader, and subsequent intact cases are still processed.

The regression fails on parent `0ef8f90` and passes after the repair. All 26
inventory tests pass, including CLI serialization, production-reader parity,
unchanged database bytes and no new SQLite sidecars. Removing only the new
reconciliation branch in isolation fails the regression; byte-identical
restoration passes. [validation.json](validation.json) binds the source, tests
and compressed red/green transcripts. Related synthetic batch (1), closer (9),
receipt (9), reader/Gate (8), evidence unittest (11) and module selftest (23
checks) runs pass. Syntax and whitespace checks pass.

The full CLI baseline still reports 51 passes and 16 failures, matching the
earlier receipts. Pytest and Black are unavailable; package installation failed.
No Python files changed and no current named pytest acceptance is claimed.

The original Brain and historical 71-case manifest remain unavailable. Inventory,
real retrospective execution and real closer observation remain unchecked.
Source issue comments supply bounded observations, not the missing cohort.
The prepared PR-body reconciliation aligns both copies of previously verified
tasks and replaces `Closes #1074` with `Tracks #1074`; automatic approval review
rejected that update, the blocker comment and `needs-human` label because
connector writes require approval while the run policy is `never`.

Repository staging also failed because `.git/index.lock` is read-only. An isolated
source commit and transfer patch are preserved under
`/tmp/1074-missing-target-delivery`; the workspace changes remain available to
automation. PR #1075 remains open and ready for review. Broad operational
acceptance still requires the original data and a provisioned closer observation.
