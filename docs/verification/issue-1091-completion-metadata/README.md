# Issue 1091: completion metadata correction

- [ ] Correct completion metadata so delivered tooling is retained without representing operational acceptance as complete.

The correction is implemented and tested as a read-only proposal generator;
publication remains blocked. No operational task is checked off.

`scripts/reconcile_adjudicator_completion.js` takes an explicitly captured
#1074/#1075 snapshot and emits proposed body patches with original-body SHA256
preconditions. It preserves delivered tooling, the checked inspection/Gate work,
and historical receipt validation. It clears report reconciliation and
current-source named-test/mutation claims in both PR checklist copies, replaces
the standalone `Closes #1074` directive with `Tracks #1074`, and adds the evidence
boundary and successor owner. Repeated generation is idempotent. The generator
never calls GitHub or opens Brain/report data; its output explicitly states that
remote metadata is unverified and operational acceptance is incomplete.

The [closer handoff](https://github.com/stranske/Orchestrator/issues/1074#issuecomment-6610044576)
explicitly moves unmet criteria to #1091. Its captured author and text are in
`handoff.json`. Consequently the final proposal preserves the closed issue
lifecycle and explains the handoff rather than reopening duplicate work. An
earlier attempted reopening was rejected before mutation and is superseded by
this body-only proposal. Merged PR #1075 remains merged. Open PR #1092 was
confirmed ready for review; its standalone `Closes #1091` also needs replacement
with `Tracks #1091` while broader acceptance is incomplete.

Reproduce the proposed source-issue and merged-PR patches:

```sh
node scripts/reconcile_adjudicator_completion.js tests/fixtures/adjudicator-completion-before.json
```

The fixture captures authenticated GitHub reads during this run; it is a test
snapshot, not permission to overwrite a newer body. Before publication, fetch
fresh bodies, regenerate the proposal, compare the expected original-body
digests, apply the corrections, then re-read all affected bodies and verify
#1092 remains open with `isDraft=false`. Only then check the metadata task.
Retain pending acceptance checkboxes. The checked receipt-retrieval task means
historical validation at `6ed980924ec1d7d83a06f9b6a181dc76ba5eb910`, not a
fresh run on the evaluated successor source.

Verification on parent `689b15cd2cdfc1a03c660f7691bc3cb840265d40` plus this change:

```sh
node tests/test_adjudicator_completion.js
node tests/test_adjudicator_receipts.js
node --test-name-pattern='retro saves Gate gaps' tests/test_adjudicator_retro_cli.js
node --check scripts/reconcile_adjudicator_completion.js
git diff --check
```

Seven new metadata regressions, nine historical receipt tests, and all four
empty-Gate recovery tests pass. The metadata regressions exercise both checklist
copies, preservation of verified work and unrelated text, repeated generation,
unexpected input rejection, lifecycle preservation, and the CLI's read-only
behavior. No Python files change. Current-source named pytest acceptance is not
claimed; the available Python interpreter has no pytest installed.

Automatic approval review rejected the #1074/#1075/#1092 metadata writes and
the #1092 blocker comment and `needs-human` label: connector mutations require
approval while the run policy is `never`. Remote metadata remains unchanged.
The final proposed patches and blocker instructions are retained under
`/tmp/1091-completion-delivery`; the source changes remain in the workspace.
The checkout's `.git` is read-only, so the meaningful source/test commit and
transfer patch use separate Git metadata in that directory.

With publication blocked, the original #428 issue and its 22 comments were
retrieved, and the cohort-related comments inspected for the next task. They provide aggregate counts and individual
case/role references, including Orchestrator #445, but no authenticated complete
71-dispute identity manifest. Counts changed to 122 in later observations; those
observations cannot establish membership in the original cohort. No recovered
identity list or operational retrospective is claimed by this round.
