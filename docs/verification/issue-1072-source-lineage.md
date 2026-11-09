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

## Foreign source receipts remain in recovery

Checked on 2026-11-09 against checkout
`f7d8e8f2234c499589f5d1b88eb23841c286bf68` plus this change.

A native receipt with a valid but contradictory source hash now retains a
`source_report_hash_mismatch` evidence gap. Previously, a validated binary
receipt could disappear from requested-case recovery despite that contradiction.
Completed receipts require source resolution before retry; earlier missing
receipt/identity steps and reservation/in-flight UNKNOWN recovery remain intact.
Matching hashes still require native authentication and independent later truth.

Two new API/CLI regressions cover original reports and retained native publication
markers. Both fail before the fix and pass afterward. They preserve recursive
paid history, original decisions, UNKNOWNs and input bytes without exposing
provider payloads. A later matching source hash clears the contradiction without
granting authentication or dispatch authority. All 205 tests pass across the
retained-report, native inventory and live-cohort JavaScript suites (baseline:
203), using `node --test --test-isolation=none --test-reporter=tap
tests/test_adjudicator_inventory.js tests/test_adjudicator_native_inventory.js
tests/test_adjudicator_live_cohort.js`. Diagnostic and replay/publication unittests
pass 32 and seven tests respectively. JavaScript syntax and `git diff --check`
pass; no Python files changed.

These are isolated fixture checks. The configured canonical report, outcomes
database and runtime directory are absent. Authentic original/journal locations
and deployment/mirror bindings were not supplied. The first unchecked task
remains incomplete; no production pin, dispatch, publication, historical proof
or live decision is claimed. The retrospective pytest command with
`-m 'not slow'` cannot start because pytest is unavailable.

Staging fails because `.git/index.lock` is read-only. A separate Git metadata
directory and `/tmp/orchestrator-1072-source-recovery/change.bundle` preserve the
focused source/test change; the checkout branch and remote are unchanged.
PR #1081 was observed open with `draft=false`. Automatic approval review rejected
the blocker comment and `needs-human` label because GitHub writes require approval
while this run's policy is `never`; neither was published.
