# Requested retry recovery dispositions

Checked 2026-11-04 UTC. Source commit `97e6396add872214bf09e81d9e48c1443546d672`
has parent `31d94f326a570cc61159cf653c3915d7c8397b89`. It adds a recovery
disposition to each supplied manifest identity in the retained-report inventory.
An unbound identity requires recovery before using candidate outcomes. A unique
match uses its primary saved attempt; historical retry failures cannot replace
its saved decision or supply its later truth.

Dispositions distinguish recorded failures, unresolved failure causes and
observed evidence gaps. Missing packet evidence is a recovery requirement, not
proof that it caused an earlier failure. Attempts with a backend identity but
no saved decision require native receipt recovery before packet recollection.
Grading gaps separately retain packet deficiencies, unauthenticated native
receipt locators and missing, UNKNOWN or unverified later truth. An ambiguous
identity has null grading gaps because its evidence cannot be attributed.
No disposition authenticates an outcome or authorizes a dispatch.

All [126 focused regressions](validation.json) pass: 71 inventory, 33 acceptance
runner and 22 ledger checks. Four new test cases cover identity gaps, failure
precedence, packet gaps, unrecognized decisions, substantive abstentions,
preservation of primary decisions and UNKNOWN, paid-attempt recovery, CLI
output and byte-for-byte preservation of reports, manifests and native journals.
The initial focused invocation failed before implementation. Final syntax and
whitespace checks pass. No Python files changed.
The [ledger audit summary](ledger-audit.json) records successful attribution and
local artifact checks; external references and operational acceptance remain
unauthenticated.

The existing command gains these fields when a manifest is supplied:

```sh
node scripts/export_adjudicator_inventory.js --report SAVED_REPORT \
  --manifest REQUESTED_MANIFEST --observed-at CHECK_TIME \
  --evidence-class historical_snapshot --evidence-reference SOURCE_REFERENCE
```

Only fixture manifests were exercised. No original retry manifest was recovered
or substituted. Original retry reconciliation, eligible native retries,
the graded >=25 intermediate threshold, canonical graded >=29 and fixed next-20
decisions remain incomplete.
Authenticated receipts, current independent truth, accepted/deployed/mirror
bindings, live writer ownership and raw October 18 proof remain unavailable.
Prior required Python test receipts remain failed before collection; this run
confirmed pytest is unavailable and did not repeat those unchanged attempts.
No task checkbox was completed, and no production mutation occurred.

Read-only GitHub inspection confirmed PR #1097 open with draft=false at checkout
head `31d94f326a570cc61159cf653c3915d7c8397b89`. The blocker comment and
`needs-human` label were rejected with "MCP tool call requires approval, but
approval policy is never". Neither remote change was applied.
Normal staging failed because checkout `.git` is read-only. The source commit
is preserved in `/tmp/issue1096-recovery-disposition-git-HuEcTt/repo.git` and the
[applyable patch](../../../../artifacts/issue-1096-recovery-dispositions.patch).
The checkout branch itself is unchanged.
