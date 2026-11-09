# Native receipt source attribution

Checked on 2026-11-09 against checkout
`3c027b6855436e198d9b0d9392f4f77a15cb245e` plus this change.

The retained-report inventory now identifies the source reference used to compare
native journal receipts. For an unpublished report it hashes the observed report
bytes. For a native canonical publication it uses the retained
`native_publication.source_report_sha256` claim. Invalid, null or missing marker
values leave that reference unavailable; they cannot fall back to the canonical
file's hash and misattribute receipts to the publication itself.

Each native attempt reports `source_binding.status`: `matches_reference`,
`differs_from_reference`, `not_recorded`, `invalid`, or `reference_unavailable`.
These diagnostics also accompany retained-attempt receipt locators and unresolved
requested-case recovery records. They compare saved hashes only. The source and
receipts remain unauthenticated, including when the hashes match. Foreign receipts,
recursive paid history and unresolved attempts remain visible; no comparison
authorizes dispatch, changes recovery state or establishes a grade.

Validation:

- Five new API/CLI cases cover original reports, canonical publications and three
  unavailable-marker forms, with matching, foreign, absent, blank and BLOB source
  hashes. The first three fail against the prior exporter. All five pass now.
- The real Python native-journal producer integration checks the same attribution
  against both original and canonical report identities, including committed WAL
  receipts. Fixtures preserve report, journal and WAL bytes.
- `node --test --test-isolation=none --test-reporter=tap
  tests/test_adjudicator_inventory.js tests/test_adjudicator_native_inventory.js
  tests/test_adjudicator_live_cohort.js`: 198 passed (baseline: 193).
- Diagnostic and replay/publication unittest suites: 32 and seven passed.
- JavaScript syntax and `git diff --check`: passed. No Python files changed.
- `python3 -m pytest tests/test_adjudicator_retro.py -m 'not slow' -q` cannot
  start because pytest is unavailable. The required retrospective result is not
  established by these fixture checks.

The configured canonical report, runtime directory and outcomes database are
absent in this runner. Authentic original/journal locations and live deployment
and mirror bindings were not supplied. No production pin, dispatch, publication,
historical proof, live decision or completed operational checkbox is claimed.
PR #1081 was observed open with `draft=false`.

Normal staging fails because `.git/index.lock` is on a read-only mount. The
verified change is preserved using isolated Git metadata and a transferable
bundle at `/tmp/orchestrator-1072-source-lineage/change.bundle`; the checkout
branch and remote are unchanged.

GitHub rejected the blocker comment and `needs-human` label because connector
writes require approval while this run's approval policy is `never`; neither
was published.
