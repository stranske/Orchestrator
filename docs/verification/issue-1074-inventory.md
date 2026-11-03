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
