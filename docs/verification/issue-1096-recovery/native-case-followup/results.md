# Case-level native recovery results

Checked 2026-11-04 UTC. Source commit `de594803fc09b553bc7fbe19c0acb978eacb8e7a`
has checkout `fd627376680615b9652134106ee1f4fd8cb6f159` as parent.
The retained-report inventory now supplies `native_case_recovery` for each
uniquely matched requested identity. It lists unresolved native reservations,
in-flight attempts (including those without backend IDs), persisted raw responses
and unrecognized states for that case, with journal locations and recovery steps.
Material revisions remain separate. Validated attempts still require native
authentication; an empty unresolved list is not dispatch permission.

These are case-level recovery candidates, not bindings to a retained invocation.
Backend receipt locators and the primary saved attempt's recovery disposition
remain separate. Ambiguous, missing and conflicting manifest identities receive
no candidate evidence. Unavailable journals produce null unresolved lists,
distinct from an available journal with no observed unresolved attempts.
Saved decisions, UNKNOWN truth and recursive paid history remain intact.

All [137 focused checks](validation.json) pass: 78 inventory, 33 acceptance-runner
and 26 ledger regressions. The three new controls fail against the prior source
with 75 existing controls passing. They verify CLI output, journal locators,
missing backend IDs, state handling, identity isolation, unavailable storage,
and byte-for-byte preservation of reports, journals and manifests. Syntax and
whitespace checks pass. No Python files changed. Required Python acceptance is
still blocked before collection by missing pytest in the selected interpreter;
fixture results do not establish accepted deployment identity.

The [pre-execution plan](plan.md) records the bounded search and local verification.
The original requested retry manifest, authenticated receipts and independent
current truth remain unavailable, as do accepted/deployed/mirror bindings,
live writer ownership and the original fixed next-20 membership. Raw October 18
proof remains unavailable separately from its retained dated observation.
Original retry reconciliation, eligible retries, intermediate graded >=25,
canonical graded >=29 and the fixed-cohort decisions remain incomplete.
No task checkbox was completed. No provider call, production write, canonical
publication or mirror synchronization occurred.

PR #1097 was checked open with `isDraft=false` at checkout head `fd627376`.
The connector rejected the blocker comment and `needs-human` label because
they require approval while this run's approval policy is `never`. Neither
remote change was applied. Normal staging failed because checkout `.git` is
read-only. Source and evidence commits are preserved in
`/tmp/issue1096-native-case-git-434rpe_u/repo.git`; the reviewable handoff is
[the applyable patch](../../../../artifacts/issue-1096-native-case.patch).
The checkout branch itself is unchanged.
