# Issue 1111: persisted role and cost references

Task 2 advances but remains incomplete. Recent commits `1021c90`, `cffc0da` and
`ee0ffdd` were reviewed before continuing; none satisfies a full operational
task. All 26 PR task/acceptance checkboxes correctly remain unchecked. The
original 71 identities remain unavailable, and the fresh
[runner inventory](runner-inventory.json) still cannot open the default Brain
(exit 2, counts null). No owner-excluded repository was accessed remotely.

The existing case-gap exporter now accepts an opt-in persisted lookup:

```text
node scripts/export_adjudicator_case_gaps.js --db BRAIN --observed-at UNIX_SECONDS --manifest ORIGINAL_MANIFEST --report SAVED_REPORT --persisted-roles
```

The lookup first reads a role's target, then requires its role kind, metadata,
backend ID and decision to match the saved claim. It retains digests of matching
stored role and cost rows within a read-only transaction. Missing records,
malformed metadata, mismatched identities and unavailable schemas become
specific per-case gaps. Excluded requested identities trigger no role lookup;
excluded stored role targets are rejected before reading metadata, and excluded
backend identities prevent cost reads. The inventory's existing owner predicate
is shared with the role reader.

Positive `ccusage` receipts are retained as measured whole-run backend costs.
Zero, ledger proxies and absent/incomplete telemetry remain UNKNOWN; no estimate
is manufactured. Differences between saved cost claims and stored receipts are
explicit. Existing behavior is preserved without the new flag, which requires
a saved report and the existing schema 2 dispute manifest.

Matching a stored role to a saved claim does not authenticate the original case
or bind its execution revision. These fields remain UNKNOWN at case level:
revision, shadow result, role-run provenance and cost. The nested evidence says
`matched_role_record_only` and `original_dispute_binding_verified=false`.
Operational acceptance and original-cohort verification remain false. No real
contested observation, original role run or original cost receipt was recovered
by these private-fixture tests.

Verification is retained in [validation.json](validation.json): the expanded
exporter suite passes 19 tests, including one using the production role recorder
and cost grader. Against the parent exporters, the same suite passes 11 and
fails 8. All six related test files, eight reader/Gate CLI checks and 23 module
self-checks pass. Syntax and diff whitespace checks pass. The named pytest
acceptance command executes zero tests because pytest is absent; neither
specified safety mutation was performed. No `.py` file changes, so the Python
formatting gate does not apply. The full CLI suite was not rerun; its two known
source failures retain their [prior dispositions](../issue-1111-cli-failures/README.md).

Fresh authenticated [metadata readbacks](metadata-readbacks.json.gz) retain bodies
and states for #1074/#1075/#1091/#1092/#1104/#1105, plus current PR #1112 and source
issue #1111. The older issues remain closed with unchecked work; their merged
PRs retain closing references. Source #1111 remains open. PR #1112 remains open,
ready for review (`draft=false`), with all 26 items unchecked and its premature
`Closes #1111` reference still present. Readbacks alone do not reconcile those
completion claims or establish successor links in their comments.

The proposed PR body changes only that closing line to `References #1111`.
Automatic approval review rejected the body update, active-thread reply and
`needs-human` label because connector writes require approval under policy
`never`. No remote mutation or reviewer-thread resolution completed.
[publication.json](publication.json), [proposed-pr-body.md](proposed-pr-body.md)
and [proposed-thread-reply.md](proposed-thread-reply.md) retain the concrete
publication handoff. The active finding remains valid.

The checkout's Git metadata is read-only: `git add` cannot create
`.git/index.lock`. The meaningful source/test commit is
`ac5a7b41f03cdc1800c564690488caf0f53e8553`, made using separate metadata under
`/tmp/issue-1111-role-delivery.git`. The complete transferable
[source commit patch](source-commit.patch.gz) is retained here; the checkout
branch was not advanced.

Successor ownership remains [issue #1111](https://github.com/stranske/Orchestrator/issues/1111)
and [PR #1112](https://github.com/stranske/Orchestrator/pull/1112). Recover the
authenticated original identity manifest and accessible owner-permitted Brain
first. Then use the retained exporters to compare saved claims with persisted
records while preserving revision-binding gaps. The named safety experiments,
real contested observation, summary/switch-review reconciliation, remaining CLI
repair and authorized completion-metadata publication are still unfinished.
