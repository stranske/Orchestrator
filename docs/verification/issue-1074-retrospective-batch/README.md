# Issue 1074: synthetic retrospective batch verification

- [x] Inspect current main and existing receipts first; retain the merged validation and regression work.
- [ ] Establish a read-only inventory of the requested 71 disputes.
- [ ] Run the retrospective with outcome writes prohibited on the requested disputes.

Current GitHub main's `src/adjudicator_retro.py` blob is
`acb3d5d40e0a1326c07dd69d95b8c0e3915797fe`, identical to this checkout. Merged
PR #511 (`b9267a0`) and the inventory, packet-validation, named-acceptance and
Gate-gap receipts were inspected. Production Python source is unchanged.

The original Brain database and requested case manifest remain unavailable,
as recorded in [the inventory observation](../issue-1074-inventory-snapshot.json).
The historical 37-of-122 summary does not identify or reconcile the requested
71 disputes. Operational inventory and replay are blocked by those inputs.
Real costs, later truth and observed closer invocation remain UNKNOWN or unrun.
No broad acceptance checkbox is advanced by this synthetic verification.

The new `tests/test_adjudicator_retrospective_batch.js` regression seeds a private
71-dispute Brain and runs nine eight-case batches through the production reader,
packet builder, adjudicator role, Brain recorder and report publisher. Routing
and provider responses are mocked. Every dispute shares one PR, so matching by
PR alone cannot pass. SQLite triggers reject INSERT, UPDATE and DELETE on
outcomes throughout replay; each prohibition is probed before execution.

Every checkpoint matches its saved report and preserves authoritative outcomes.
All 71 case identities survive batching and resume. The final report retains
57 distinct role runs, including 14 invalid provider responses, plus 14 explicit
Gate evidence gaps. There are 29 binary proposals and 14 abstentions. Independent
per-case truth and merge comparisons agree with the saved aggregates. Metadata
proposals remain ungraded. Costs are measured for 43 calls (a $100 subtotal);
14 dispatched calls retain null dollars and `unmeasured` status. The weekly line
agrees with those counts. Resume adds no calls or role records.

The new regression, seven inventory tests, eight focused inventory/Gate tests,
23 retrospective selftest checks and 11 evidence unittest tests pass. Before
the change, the complete existing CLI suite still reports 51 passes and 16
failures; the receipt retains their names. This is not a successful full verifier
or named pytest acceptance run. Pytest and Black are unavailable. No `.py` file
changes, and pytest collection/floor metadata is unchanged.

Three isolated production-source faults fail the new regression: an outcome
UPDATE that preserves the original value, dropping saved evidence gaps, and
converting missing cost telemetry to measured zero dollars. Byte-identical
restoration passes. These are synthetic regression controls, not the two required
named acceptance mutations. [The receipt](receipt.json) binds source/test hashes,
commands, logs and the precise fault patches.

Git staging failed because `.git/index.lock` cannot be created on the read-only
mount. The tested changes are preserved in an isolated source commit and transfer
patch under `/tmp/1074-retrospective-delivery`; workspace files remain available
for automation to collect. PR #1075 was confirmed open and ready for review.
The connector rejected the verified inspection/Gate checkbox reconciliation,
removal of `Closes #1074`, blocker comment and `needs-human` label: mutations
require approval while this run's policy is `never`. Remote metadata therefore
still needs correction; operational tasks remain incomplete.
