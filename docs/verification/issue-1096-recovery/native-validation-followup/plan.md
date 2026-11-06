# Native validation recovery plan

Recorded 2026-11-04 UTC before implementation against checkout
`21e5f87cf5a69c448009ad111e6f7a455069d005`.
The first unchecked task remains original retry reconciliation. Original manifest,
authenticated native receipts, independent later truth and live writer ownership
remain unavailable; fixture verification cannot complete that task.

`NativeAttemptJournal.validate` saves failed results and abstentions under state
`validated`. The inventory currently drops every such row from unresolved case
recovery. Read saved validation results into sanitized observations, exposing
recorded failures, abstentions and malformed/missing evidence. Preserve journal
locations and hashes without publishing raw responses, error prose or proposals.
Only an observed binary result without errors and with receipt fields present
may leave the unresolved list; it still requires native authentication.

Run the inventory regressions before implementation, add controls that fail on
the prior source, then run inventory, acceptance-runner and ledger regressions.
Verify CLI output and byte preservation of report, manifest and journal. Record
source hashes and exact command output. Stop after local verification: no paid
dispatch, production writes, canonical publication or replacement cohort.
The selected Python lacks pytest; existing required-test failures remain open.
No Python changes are planned, so the Python-specific Black gate does not apply.
