# Issue 1074: closer shadow hook regression

- [x] Inspect current main and existing receipts first; retain the merged validation and regression work.
- [x] Guard empty commits.nodes in both initial and paginated gate reads, record a per-case evidence gap, and continue the batch; add regressions for both paths.
- [ ] Establish the read-only inventory of the requested 71 disputes.
- [ ] Run the retrospective on the requested disputes with outcome writes prohibited.
- [ ] Demonstrate live-shadow invocation at a real contested closer disposition.

Recent commits `d470db0`, `826edfb` and `75048c0` and their receipts were reviewed
before implementation. The inventory exporter and synthetic retrospective batch
do not establish the missing historical cohort. Both the default Brain database
and saved retrospective report are still absent. Real case identities, native
costs, later-truth comparisons and observed closer invocation remain unavailable.
No broad acceptance checkbox is advanced here.

The current main version of `merge_guard.adjudicate_disagreement` was inspected
through GitHub. Its production hook already invokes the adjudicator selector
for a conclusive runtime-gate/panel disagreement. The existing panel regression
stubs activation; the new `tests/test_adjudicator_closer_shadow.js` exercises
`guarded_merge` through the real selector, role runner, provider-response parser,
Brain role recorder and selector-event recorder. Python source is unchanged.

Every test uses a private Brain and fixture metadata, runtime gate, panel,
capacity and provider. SQLite triggers prohibit INSERT, UPDATE and DELETE on
outcomes; all three prohibitions are probed, including a no-op UPDATE. An injected
final preflight refuses before any merge command. The fixture proves that shadow
advice cannot waive this deterministic refusal. No actual merge is attempted.

Nine tests pass: a contested verdict persists one linked role record; agreeing,
inconclusive and missing panel verdicts cause no routing, invocation or role
record; disabled shadow, dry-run and unavailable capacity withhold dispatch;
invalid output and a provider exception preserve the final refusal. Every case
preserves authoritative outcomes and the original run. The role-run ID and
backend ID, accepted selector event and saved proposal are checked independently.
This is fixture evidence, not a real contested closer disposition.

Three isolated production faults fail these tests: attempting a no-op outcome
write, invoking on agreeing verdicts, and omitting the closer hook. Exact source
restoration passes all nine tests. The [receipt](receipt.json) records hashes,
commands, compressed transcripts and exact fault patches. These controls are
not the required named pytest acceptance mutations.

The existing synthetic batch test and nine focused inventory/Gate/offer tests
also pass. The merge-guard selftest passes; the retrospective selftest passes
23 checks, and the evidence unittest suite passes 11 tests. JavaScript syntax
and whitespace checks pass. Pytest remains unavailable; no current named pytest
or full verifier success is claimed. The earlier full CLI receipt retains its
16 existing failures. No Python files or pytest collection metadata change.

PR #1075 was open and ready for review when inspected. Reconciliation attempted
to check both copies of the inspection and Gate tasks/criterion and replace
premature `Closes #1074` with `Tracks #1074`. The connector rejected the PR-body
update, blocker comment and `needs-human` label because mutations require
approval while this run's approval policy is `never`. The checklist above is
local verified state; the remote PR still needs correction.

Repository staging failed because `.git/index.lock` is on a read-only mount.
An isolated source commit and transfer patch are prepared under
`/tmp/1074-closer-delivery`; workspace changes remain available for automation.
The original Brain/cohort and a real closer observation are needed to finish
operational acceptance.
