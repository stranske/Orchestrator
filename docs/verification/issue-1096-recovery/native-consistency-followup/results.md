# Native receipt consistency recovery results

Checked 2026-11-04 UTC. Source commit
`bd84c68e06ef5ef02ce6a2bfe3966ab42ab0b9b5` has checkout
`5b761ed855fbbe215c99487f67d9dd5dc1b256c8` as parent.

Observed binary proposals no longer disappear from unresolved case recovery
when their saved backend identity or raw output is absent, malformed or
contradicts the journal. Both equalities are required by the native validator.
Raw response comparison preserves whitespace and Unicode exactly and does not
parse provider output. Missing fields and mismatches have separate diagnostic
codes. Matching fields still require native authentication, proposal validation,
case qualification and independently refreshed later truth before grading.

All [143 focused regressions](validation.json) pass: 84 inventory, 33 acceptance
runner and 26 ledger checks. The three added controls fail against the prior
source while all 81 existing inventory checks pass. They cover absent and
malformed fields, conflicting IDs, changed responses, exact empty/Unicode/text
matches, concurrent gaps, CLI/API parity, privacy, per-attempt separation and
unchanged report/manifest/journal bytes. Parent decisions, UNKNOWN truth and
recursive paid history are preserved. Syntax and whitespace checks pass.
No Python files changed; the Python-specific Black gate does not apply.

The [recorded plan](plan.md) precedes implementation and the
[bounded acceptance attempt](acceptance-run/checks.json) binds the exact source
commit, selected interpreter, commands, logs and file hashes. Collection, all
four named tests and the full retrospective suite fail before collection:
the selected Python 3.14.10 interpreter lacks pytest. This is not passing test
or accepted deployment evidence.

[Current external recovery observations](recovery-observation.json) remain
separate from historical native receipts and ownership notes. Only fixture
inputs exercised the implementation. Original requested retry membership,
native authenticated receipts, independent current truth, accepted/deployed/
mirror bindings, current writer ownership and fixed next-20 membership remain
unavailable or unverified. Raw October 18 proof remains unavailable separately
from its retained dated observation. No new task checkbox is complete.
Original retry reconciliation, eligible retries, intermediate graded >=25,
canonical graded >=29 and fixed-cohort decisions remain incomplete.
No paid dispatch, canonical publication or mirror synchronization occurred.

PR #1097 was observed open with `draft=false`. Normal staging failed because
checkout `.git` is read-only. The source commit is preserved in
`/tmp/issue1096-native-consistency-git-sjri2uw6/repo.git` and the
[applyable source patch](../../../../artifacts/issue-1096-native-consistency.patch).
The checkout branch itself is unchanged. Connector mutations for the blocker
comment and `needs-human` label were rejected because they require approval
while policy is `never`; neither remote change was applied.
