# Native validation recovery results

Checked 2026-11-04 UTC. Source commit
`c6730889c4b446aaa33e11dcee2761b4e4f3c581` has checkout
`21e5f87cf5a69c448009ad111e6f7a455069d005` as parent.

The native journal's `validated` state records failed results and abstentions as
well as binary proposals. The inventory previously omitted all of them from
unresolved case recovery. It now retains failed, abstained, conflicting,
unrecognized and missing decisions, along with malformed or absent validation
data and missing raw receipt/backend identity fields. Saved native validation
diagnostics carry exact stored-text hashes, field locations and the first
recorded broken step when repository messages establish one. Unknown failure
prose remains unresolved. Provider text and proposals are not published.

These are observations of journal-owned fields, not authenticated outcomes.
An observed binary proposal with receipt fields present still requires native
authentication. Case-level recovery stays separate from backend-bound receipt
locators and from the primary saved decision; parent decisions and UNKNOWN truth
remain intact. No observation grants dispatch permission.

All [140 focused regressions](validation.json) pass: 81 inventory, 33 acceptance
runner and 26 ledger tests. Three new controls failed against the prior source
with the 78 existing inventory tests passing. They verify SQLite and CLI output,
failure-stage precedence, abstentions, conflicting and malformed results,
missing receipt fields, privacy and byte preservation of the report, journal and
manifest. Syntax and whitespace checks pass. The [ledger audit](ledger-audit.json)
passes attribution and local artifact checks; operational acceptance remains
unverified. No Python files changed.

Only fixture inputs were exercised. The original requested retry manifest,
authenticated receipts, independent current truth, accepted/deployed/mirror
bindings, live writer ownership and original fixed next-20 membership remain
unavailable. Raw October 18 proof remains unavailable separately from its
retained dated observation. Required Python acceptance remains blocked by
missing pytest in the selected interpreter. No additional task checkbox was
completed; original retry reconciliation, eligible retries, intermediate graded
>=25, canonical graded >=29 and fixed-cohort decisions remain incomplete.
No paid dispatch, production mutation, canonical publication or mirror sync
occurred.

PR #1097 was observed open with `draft=false` at checkout head `21e5f87`.
Normal staging failed because checkout `.git` is read-only. The code commit is
preserved in `/tmp/issue1096-native-validation-git-7jtp1W/repo.git` and the
[applyable source patch](../../../../artifacts/issue-1096-native-validation.patch).
The checkout branch itself is unchanged. The [pre-execution plan](plan.md) and
hashed test logs preserve the verification boundary.
