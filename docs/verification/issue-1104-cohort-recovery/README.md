# Issue 1104: cohort recovery and owner exclusion boundary

- [ ] Recover the authenticated requested cohort from original evidence and reconcile it with the permitted inventory. Record missing identities, exclusions and count discrepancies explicitly; do not infer original membership from later aggregate counts or inspect owner-excluded repositories.

The original cohort remains unrecovered. The authenticated GitHub connector
retrieved the originating issue #428 and all 22 comments. Their exact text,
authors and URLs are retained in [source-428.json](source-428.json). The issue
requests 71 disputes but supplies aggregate categories, not their identities.
The external local audit it cites is absent from this checkout; its authenticated
repository location and complete source revision remain unknown.

[reconciliation.json](reconciliation.json) records zero recovered original
identities and 71 unidentified requested slots. This is a recovery ledger, not
an empty manifest that could substitute for the requested cohort. The later
`keepalive:stranske/Orchestrator#445:codex` reference is retained separately with
original membership and verdict pair UNKNOWN. The 95- and 122-case observations
and saved 37-of-122 summary have no recovered identity mapping to the original
71. Their differences are explicit; none determines original membership.

The [fresh runner observation](runner-inventory.json) again cannot open the
default Brain. Observed counts, exclusions and count delta are null. No complete
owner-permitted inventory was recovered, so historical reconciliation and
per-case execution cannot be claimed. Only this repository's remote issue and
PR metadata were inspected; no owner-excluded repository was opened.

The concrete source change closes a permission gap in the existing exporter.
The owner exclusion for `stranske/Collab-Admin`, already recorded in
`src/feature_goals.py` and the reviewed publisher exclusion stage, now applies
before outcome evidence is joined. Excluded runs contribute only their run ID,
target and `owner_excluded_repository` reason. Their timestamps and outcome
fields are not selected. Counts still account for these exclusions, and the
eligible-count discrepancy remains visible. Both manifest versions retain
requested exclusions even when the Brain is unavailable or the stored run has
another target. The report lists the applied exclusion policy and continues to
set `requested_cohort_verified=false`.

Two new regressions fail against parent source and pass after this change. One
uses private SQLite views that raise if an excluded timestamp or verdict is
evaluated. It also checks mixed-case repository identity, a permitted name
lookalike, count discrepancies, manifest reconciliation, CLI output, unchanged
input bytes and no report or SQLite sidecars. The other covers excluded
requested identities with absent and contradictory observations in both schema
versions. These synthetic rows establish the reader boundary only.

Verification retained in [validation.json](validation.json):

```sh
node tests/test_adjudicator_inventory.js
node --test tests/test_adjudicator_inventory.js tests/test_adjudicator_retrospective_batch.js tests/test_adjudicator_closer_shadow.js tests/test_adjudicator_receipts.js tests/test_adjudicator_completion.js
node --test-name-pattern='dispute inventory|retro saves Gate gaps' tests/test_adjudicator_retro_cli.js
python3 src/adjudicator_retro.py --selftest
PYTHONPATH=src python3 -m unittest discover -s tests -p test_adjudicator_retro_evidence.py
```

All 28 inventory tests pass; all five related test files pass. The focused
reader/Gate tests pass eight tests, the module selftest passes 23 checks and
the evidence suite passes 11 tests. JavaScript syntax and diff whitespace checks
pass. No Python files change. Pytest is unavailable, and no fresh named pytest
acceptance, requested mutation proof, full verifier or disposition of the
retained 16 CLI failures is claimed. No operational checkbox is completed.

The checkout's Git metadata is read-only: staging cannot create
`.git/index.lock`. A meaningful source/test commit and transfer patch are
prepared with separate Git metadata under `/tmp/issue-1104-cohort-delivery`;
workspace changes remain available for automation to collect. The missing
original manifest, original Brain and permitted inventory remain the recovery
blockers; safe reader tooling does not resolve them.

Automatic approval review rejected the PR #1105 blocker comment and
`needs-human` label: connector writes require approval while this run's policy
is `never`. Neither was published. A final authenticated read confirmed PR
#1105 is open with `draft=false`; its operational checkboxes remain unchecked.
