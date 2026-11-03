# Issue 1074: inventory reader verification, 2026-11-03

- [x] Inspect current main and existing receipts first; retain the merged validation and regression work.
- [ ] Establish the read-only inventory of the requested 71 disputes.
- [x] Guard empty initial and paginated gate commit lists, record a per-case gap, and continue the batch.

The checkout contains merged PR #511 and subsequent gate recovery changes. The
GitHub main version of `src/adjudicator_retro.py` has blob
`acb3d5d40e0a1326c07dd69d95b8c0e3915797fe`, matching this checkout. Python source
and the existing packet validation regressions are unchanged in this round.
The existing Node witnesses verify empty initial and paginated commits, null
rollups, subsequent-case execution, unchanged outcomes, and explicit retry.

The original Brain database and canonical retrospective report are unavailable
in this runner. Neither default path exists, and the checkout contains no real
Brain database or original 71-case manifest. This is unavailable evidence, not
an observed zero-case inventory. No production replay or closer disposition was
performed. Case identities, exclusions, per-case evidence gaps, actual costs and
later-truth comparisons for the requested cohort remain UNKNOWN.

The historical
[`private-replay-summary.json`](../evidence/issue-428-metadata-floor-20261006/private-replay-summary.json)
reports 37 cases from a population of 122, all metadata-only, with no measured
costs or graded cases. It supplies neither the requested 71 identities nor a
mapping explaining the population difference. Its counts cannot establish the
original cohort's completeness. The existing
[named-test receipts](issue-428-named-acceptance-20261007/validation.json) describe
revision `6ed9809`; they are historical evidence, not a fresh acceptance run on
this source.

Four new Node-driven tests exercise the real `disputes` reader against private
SQLite fixtures. The synthetic 71-row fixture preserves distinct same-PR case
identities, an inclusive 90-day boundary, deterministic timestamp ties and fixed
observation time. It explicitly excludes old rows, agreeing verdicts, missing
verdicts and runs without outcomes. The other fixtures distinguish an absent
database and missing schema from an available empty Brain. Reads use SQLite's
read-only mode, preserve database bytes, and create no report or missing store.
These fixtures do not reconstruct the historical inventory or add a production
inventory exporter.

Verification:

```sh
node --test-name-pattern='dispute inventory|retro saves Gate gaps' tests/test_adjudicator_retro_cli.js
python3 src/adjudicator_retro.py --selftest
PYTHONPATH=src python3 -m unittest discover -s tests -p test_adjudicator_retro_evidence.py
```

The focused Node run passes eight tests; the module selftest passes 23 checks;
the evidence unittest suite passes 11 tests. Five isolated reader mutations
(exclusive cutoff, inclusion of agreements, missing tie ordering, writable
connection, and per-PR collapse) each fail a new inventory test. Byte-identical
restoration passes all four inventory tests. These are inventory controls, not
the two named acceptance mutations. The receipt is in
[`issue-1074-inventory-validation.json`](issue-1074-inventory-validation.json).

The complete existing Node suite was also run before this change: 47 passed and
16 failed. After the change, 51 pass and the same 16 fail. Those failures include
report-shape expectations and late costs; they prevent a claim that the current
suite passes. Pytest and Black are absent;
installation into `/tmp` failed because PyPI DNS resolution is unavailable.
No Python files change, and no fresh named pytest or full verifier success is
claimed. Operational inventory, retrospective execution, observed live-shadow,
current named acceptance and actual report reconciliation remain incomplete.

The checkout's Git directory is read-only: `git add` failed creating
`.git/index.lock`. The reviewed changes are preserved in an isolated commit and
transfer patch under `/tmp`; the workspace edits remain available to automation.
GitHub rejected the PR-body correction, blocker comment and `needs-human` label
because connector mutations require approval while this run's approval policy
is `never`. The PR's existing `Closes #1074` preamble is still present and should
be removed until operational acceptance is met. Verified checkbox state above
is local only. PR #1075 was confirmed open and ready for review.

## Follow-up: read-only inventory export

`scripts/export_adjudicator_inventory.js` now exports a fixed-observation-time
Brain inventory using Node's built-in SQLite reader (Node >=22.13). It requires
an explicit existing database path and observation timestamp. It opens SQLite
read-only, reads runs and outcomes in one statement, and prints JSON to stdout.
It performs no replay, dispatch, schema initialization or report publication.

The export retains every eligible run, including multiple disputes on one PR,
and lists each excluded run with its reasons: outside the time window, missing
timestamp or outcome, missing verdicts, or agreeing verdicts. The lower cutoff
is inclusive; like the production reader, there is no upper timestamp bound.
It records population/included/excluded counts and the difference from the
requested count. A matching count cannot establish the historical identities:
`requested_cohort_verified` remains false, with an explicit evidence gap.
Unavailable stores or schemas produce null cases/counts and exit 2; an available
empty Brain produces zero observed cases and an inventory discrepancy.

The saved
[`issue-1074-inventory-snapshot.json`](issue-1074-inventory-snapshot.json)
records a real read attempt against this runner's default Brain path. The
database is unavailable. This is a runner observation, not a reconstruction of
the October audit cohort. The historical 71 identities, exclusions and mapping
to the older 37-of-122 summary remain unavailable, so the inventory task stays
unchecked. Run on the existing Brain host with a justified observation time:

```sh
node scripts/export_adjudicator_inventory.js --db /path/to/existing/brain.db --observed-at UNIX_SECONDS > inventory.json
node tests/test_adjudicator_inventory.js
```

Seven new tests pass, including production-reader parity, complete exclusion
accounting, count mismatches in either direction, missing/schema-less/empty
stores, unchanged database bytes, and CLI error handling. The existing eight
focused inventory/gate tests, 23 module selftest checks and 11 evidence tests
also pass. No Python files changed; Black and pytest remain unavailable.
The complete existing CLI suite still reports 51 passes and 16 failures; its
failure names exactly match the preceding receipt. Four isolated export
mutations (exclusive cutoff, writable store, per-PR collapse and suppressed
count discrepancy) fail the new tests, and byte-identical restoration passes.
These are inventory controls, not the named operational acceptance mutations.
See [`issue-1074-inventory-export-validation.json`](issue-1074-inventory-export-validation.json).

Task reconciliation was attempted again after verification: both copies of the
inspection task, the gate-guard task and its acceptance criterion can be checked.
The connector again rejected the PR-body mutation under the `never` approval
policy; attempts to add the blocker comment and `needs-human` label were also
rejected. The PR body and premature `Closes #1074` line therefore remain unchanged
remotely. This export advances inventory tooling only; operational inventory,
retrospective execution and live-shadow acceptance remain incomplete.

Staging in this checkout again failed because `.git/index.lock` cannot be
created on the read-only filesystem. A source commit and transfer patch are
prepared using a separate Git directory under `/tmp/1074-inventory-delivery`;
the workspace edits remain available for automation to collect.

## Follow-up: outcomes without run records

The inventory previously omitted every outcome whose matching run record was
absent. The Brain schema does not enforce that relationship. The exporter now
reads runs and unmatched outcomes in one read-only statement. `population_count`
includes both; `orphaned_outcome_count` distinguishes these outcomes from complete
run/outcome pairs and remains null when the database is unavailable.

Each unmatched outcome is an exclusion with `missing_run` and `missing_timestamp`
reasons and a per-case evidence gap. Its target and time-window membership remain
unknown. A supplied manifest entry backed by such an outcome is `unavailable`,
rather than missing or associated with a different target. Eligible replay cases
still match the production reader; no orphan outcome is dispatched or repaired.

The two new regressions fail against parent `3a31863` and pass against the changed
exporter. They exercise a mixed population, an outcomes-only store, manifest
reconciliation, CLI serialization, unchanged database bytes and no SQLite
sidecars. All 16 inventory tests pass. The related synthetic batch (1), closer
(9), receipt (9), and focused reader/Gate (8) tests pass, as do 11 evidence
unittests and 23 retrospective selftest checks. Syntax and whitespace checks pass.
The [receipt](issue-1074-orphan-outcomes.json) binds the old/new source and test
hashes to the two regressions' actual failure/pass output. Python files are
unchanged. Current-source named pytest and full verifier success are not claimed;
pytest is absent and the prior full CLI suite's 16 failures remain unresolved.

The original Brain database and historical 71-case manifest are still unavailable.
Operational inventory, real replay, observed closer invocation and actual report
reconciliation remain unchecked. Reviewed source-issue comments provide bounded
historical observations, not the missing complete identity manifest.

The attempted PR-body reconciliation checks both copies of the verified
inspection, Gate recovery and historical receipt tasks/criteria, leaves operational
tasks unchecked, and replaces `Closes #1074` with `Tracks #1074`. Automatic approval
review rejected that update, the blocker comment and the `needs-human` label:
connector writes require approval while this run's approval policy is `never`.
Remote metadata is unchanged. The prepared body is retained under
`/tmp/1074-inventory-orphan-delivery/pr-body.md`.

Workspace staging failed because `.git/index.lock` is read-only. A source commit
and transfer patch are prepared with separate Git metadata under
`/tmp/1074-inventory-orphan-delivery`; workspace edits remain available to automation.

## Follow-up: distinguish missing verdict evidence from contradictions

Version 2 reconciliation now marks a run with a missing outcome or verdict as
`unavailable`, preserving its observed exclusion reasons in both the requested
case and its evidence gap. Previously these entries were reported as
`requested_verdict_mismatch`, concealing the missing evidence. Actual target
mismatches and complete contradictory verdict pairs still report identity
mismatches; legacy run/target manifests retain their existing exclusion behavior.

All 25 inventory tests pass. The new missing-evidence regression fails on parent
`484c5a5` and passes after the repair; a second regression preserves target
mismatch precedence. CLI serialization, database bytes and absence of new SQLite
sidecars are checked. The related synthetic batch (1), closer (9), receipt (9)
and focused reader/Gate (8) tests pass, as do 11 evidence unittests and 23 module
selftest checks. No Python files change. Current named pytest and the full
verifier were not run; pytest remains unavailable. The prior full CLI suite's
16 failures remain outside this repair.

The requested historical inventory remains unchecked: the original Brain and
71-case manifest are still unavailable. This change does not run a historical
replay or observe a real closer disposition. PR #1075 was verified open and ready
for review. Staging again failed because the checkout's Git metadata is read-only;
the source commit and transfer patch are preserved under
`/tmp/1074-missing-verdict-3_948qie` for automation to collect.
The connector rejected this round's blocker comment and `needs-human` label
because writes require approval while the run's policy is `never`; neither was
published. The PR's existing premature closing reference remains unchanged.
