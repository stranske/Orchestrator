# Fixed live-cohort observation

Checked on 2026-11-05 against checkout
`62c064c5af7da58affd4a42d9deb62882d784c6d` plus the accompanying JavaScript changes.

`scripts/export_adjudicator_live_cohort.js` observes an already fixed 20-member
manifest against the existing `adjudicator-live/v1` report. It uses the same
schema-version-2 dispute identities as the inventory exporter and reuses its
owner exclusion policy. It requires an expected manifest SHA-256 and a source
reference; neither authenticates the manifest's original membership. It never
selects a replacement cohort or calls the live dispatcher.

```sh
node scripts/export_adjudicator_live_cohort.js \
  --manifest /absolute/retained-next-20.json \
  --expected-manifest-hash RETAINED_MANIFEST_SHA256 \
  --manifest-reference ORIGINAL_MEMBERSHIP_REFERENCE \
  --report /absolute/capability-program/adjudicator-live.json \
  --evidence-class current_snapshot \
  --evidence-reference LIVE_WRITER_REPORT_REFERENCE \
  --observed-at UNIX_SECONDS
```

JSON goes to stdout. Preserve each observation separately. Every requested
member retains its place, checked date and either a specific gap or a report-row
locator. Report and row hashes, attempt dates and role/backend locators make
later observations comparable. Missing reports exit 2 without creating state;
invalid or changing inputs exit 1 without a partial observation. Ambiguous rows,
changed identities, retrospective rows, abstentions, invalid dates, incomplete
certification and reused receipt locators cannot increase the retained binary
decision count. Saved aggregate counts and later truth never supply a decision.

`retained_binary_decisions` describes saved claims with dated locators. It is not
an authenticated acceptance count. The exporter leaves original membership,
dispatch authentication, historical proof and operational acceptance unverified.
Production authentication must still use the existing native evidence paths.

Verification:

- All 115 tests passed across `tests/test_adjudicator_live_cohort.js`,
  `tests/test_adjudicator_inventory.js` and `tests/test_adjudicator_native_inventory.js`
  using `node --test --test-isolation=none`.
- The 12 new tests include the real Python `run_live` producer with an isolated
  Brain, exact-revision toy repository and stubbed provider: 20 attempts retain
  15 binary decisions and five abstentions; a second pass makes no new attempt.
  The observation preserves report, manifest and Brain bytes. These are fixtures.
- The diagnostic and replay/publication unittest suites passed 32 and seven tests.
- JavaScript syntax and whitespace checks passed. No Python files changed.
- `python3 -m pytest tests/test_adjudicator_retro.py -m 'not slow'` cannot start
  because pytest is absent. Required retrospective acceptance is unverified.

The configured live report, canonical retrospective report and Brain database
remain absent in this runner. The original fixed next-20 manifest has not been
recovered. No production decision, retry, canonical refresh, deployment or mirror
synchronization is claimed, and the live-cohort task remains unchecked.

An authentic retained [October 18 scheduled observation](https://github.com/stranske/Orchestrator/issues/575#issuecomment-6251544073)
was re-read through the GitHub connector. It reports zero live advice and two
failed live cases. That historical observation remains separate from the new
fixture checks. Raw October 18 acceptance receipts and original fixed-cohort
membership remain unavailable; the dated aggregate does not supply either.

PR #1081 was observed open with `draft=false`. Shared Git metadata is read-only;
the tested source is preserved in a separate local commit and transferable
`/tmp/orchestrator-live-cohort.bundle`, with working files available for automation.
Automatic approval review rejected the blocker comment and `needs-human` label:
GitHub writes require approval while this run's policy is `never`. Neither was
published, and no remote checkbox was changed.

## Report source attribution

Checked on 2026-11-05 against `7ff5fe1` plus this change. Observations now require
`--evidence-class historical_snapshot|current_snapshot` and `--evidence-reference`,
matching the existing retrospective inventory's source attribution. The report
locator retains those declarations and its byte length alongside the SHA-256.
`checked_at` dates the supplied read; `report.generated_at` remains a saved claim.
Neither a historical source declaration nor a current read authenticates the
report's historical date, membership or dispatch receipts. Missing historical
reports retain their source reference, all 20 obligations and exit status 2.
Mirror synchronization and dispatch receipts cannot be used as report classes.

All 15 live-cohort tests pass, including three new checks for distinct historical
and current source references, missing historical reports and rejected attribution.
The same three tests fail against the original exporter in an isolated copy.
The existing producer fixture still retains 15 binary decisions and five
abstentions without supplying authentic acceptance evidence. JavaScript syntax
and diff whitespace checks pass; no Python files changed. The inventory suites
pass all 103 tests, and the diagnostic and replay unittest suites pass 32 and
seven tests. The required retrospective pytest command with `-m 'not slow'`
cannot start because pytest is missing.

The configured live report, canonical report and Brain database are still absent.
Connected-source searches did not recover the original fixed next-20 manifest.
The earlier retained October 18 scheduled observation remains attributable to its
linked source above; raw authenticated historical proof is unavailable. No
production decision, dispatch or task completion is claimed by this change.

Staging failed because the checkout's Git metadata is read-only. The focused
source, tests and note are preserved in a separate commit and transferable
`/tmp/orchestrator-live-cohort-attribution/change.bundle` and `change.patch`.

## Conflicting native dispute targets

Checked on 2026-11-05 against `bf194ef4678386a1275a27473ee2685c80b0cc51`
plus this change. The observer now groups report rows by the native
`adjudicator_retro._case_identity` (run and verdict pair), then checks targets.
A row for a different target blocks that member with `live_target_mismatch`,
even when another row matches the manifest and has distinct receipt locators.
It cannot supply a decision or a selected report-row receipt. Separate verdict
pairs on one run remain separate obligations; owner exclusions still precede
dispute and execution evidence access.

The new regression fails against the old observer (`15 != 14`) and passes with
the fix in either report-row order. It preserves all 20 pinned identities and
both input files. An additional access-trap test verifies excluded aliases do
not expose dispute or receipt fields. All 120 tests pass across the live-cohort,
inventory and native-inventory JavaScript suites; the diagnostic and replay
unittest suites pass 32 and seven tests. JavaScript syntax and `git diff --check`
pass. No Python file changed. The required
`python3 -m pytest tests/test_adjudicator_retro.py -m 'not slow'` command cannot
start because pytest is absent.

The configured local runtime, Brain and report files remain unavailable.
GitHub issue/comment reads and connected Dropbox searches for the live report
and cohort did not recover a fixed next-20 manifest or raw production receipts.
The retained October 18 observation linked above was re-read: it reports zero
live advice and two failed certified cases. This is historical aggregate
evidence, separately attributable from today's fixture verification; it supplies
neither cohort membership nor authenticated receipts. No dispatch, canonical
refresh, mirror synchronization or operational task completion is claimed.
The live-cohort checkbox remains unchecked. PR #1081 was observed open and
`draft=false` at the revision above.

Staging was refused because `.git/index.lock` is on a read-only filesystem.
The tested source, tests and this note are preserved in a separate local commit
and transferable `/tmp/orchestrator-live-cohort-targets/change.bundle` and
`change.patch`; the working files remain available to the automation runner.

## Dated retry observations

Checked on 2026-11-05 against `69329cc8bc481e1ccdf05958b8629c39d4b41480`
plus this change. Report-row locators now retain the native writer's saved
`first_attempted_at`, `attempts` and certification `evaluated_sha` alongside
the latest attempt date and role/backend locators. Invalid supplied counters
or counters without valid first/latest attempt dates block the decision.
An absent legacy counter stays null; no earlier receipt is reconstructed.

Both new tests fail against the prior observer. The native two-pass fixture
first retains 15 decisions and five backend failures, then retries only those
five. It verifies the same pinned 20 identities, unchanged initial decisions,
distinct retry receipt locators and dates, and no third-pass dispatch. Its
later truth remains absent, and authentication/acceptance flags remain false.
These are isolated fixture results, not production evidence.

All 122 JavaScript live-cohort/inventory/native-inventory tests pass, as do
32 diagnostic and seven replay/publication unittest tests. Syntax and whitespace
checks pass. No Python files changed. The required retrospective pytest command
with `-m 'not slow'` cannot start because pytest is absent.

The configured runtime directory remains absent, and original fixed membership
and native production reports were not recovered from the checkout or source
issue discussion. The linked October 18 observation above was re-read: it
reports zero live advice and two failed cases. Raw authenticated historical
receipts remain unavailable. No operational task is marked complete. PR #1081
was checked open with `draft=false` at the revision above.

Normal staging failed on the read-only `.git/index.lock`. The source, tests and
this note are preserved in an isolated local commit and transferable
`/tmp/orchestrator-live-cohort-retry-dates/change.bundle` and `change.patch`.

## Failed role-record persistence

Checked on 2026-11-05 against `2df49bad30c95745de3de84862c571e2868a2215`
plus this change. The observer now reports `role_record_write_failed` when
the native live writer retains `role_record_error`. It preserves the paid
decision, backend locator and attempt date, but an accompanying role locator
cannot override the recorded write failure or increase the binary decision
count. Private error text and the unwritten role-record payload stay omitted.

Both regressions failed against the previous observer. The isolated native
`run_live` fixture retains a paid response with a failed role write, makes no
second provider call, and produces the explicit persistence gap without
inventing a role receipt. Another fixture drops from 15 to 14 retained decisions
on the failure, then returns to 15 when a later supplied report clears it,
preserving decision and attempt identity. These fixtures do not authenticate
production evidence or perform role-record recovery.

All 124 live-cohort, inventory and native-inventory JavaScript tests pass;
the diagnostic and replay-revision unittest suites pass all 39 tests.
JavaScript syntax and `git diff --check` pass. No Python files changed.
`python3 -m pytest tests/test_adjudicator_retro.py -m 'not slow'` remains blocked
because pytest is absent.

The default runtime, Brain, live report and canonical report remain unavailable.
The original fixed next-20 manifest was not recovered from the checkout or
source issue discussion. The retained October 18 observation linked above was
re-read: it reports zero live advice and two failed cases. Raw authenticated
historical receipts remain unavailable. No dispatch, canonical publication,
mirror synchronization or operational checkbox completion is claimed.

Staging failed on the read-only `.git/index.lock`. The source, tests and this
note are preserved in a separate local commit and transferable
`/tmp/orchestrator-live-role-persistence/change.bundle` and `change.patch`.
PR #1081 was checked open with `draft=false`. Approval checks rejected the
blocker comment and `needs-human` label because GitHub writes require approval
while this run's policy is `never`. Neither was published; no remote checkbox
was changed.

## Conflicting evaluated revisions

Checked on 2026-11-05 against `fff6ad3b502957408dcc2689ba20bba58a83c4c7`
plus this change. The observer now cross-checks supplied finding, merge and
collection revision claims in both saved packets against certification.
Malformed claims produce `revision_evidence_invalid`; different commits produce
`revision_evidence_mismatch`. The paid decision and dated receipt locators stay
visible, but conflicting rows cannot increase the retained decision count.
Missing legacy claims remain absent. Moving PR heads, comparison parents and
later truth do not supply the evaluated revision or authenticate a receipt.

Both regressions fail against the prior observer. The native producer fixture
retains 15 decisions, then drops to 14 when only one certification SHA changes.
The additional test checks all six saved revision locations, malformed values,
unchanged input bytes and fixed membership. All 125 live-cohort, inventory and
native-inventory JavaScript tests pass, along with 32 diagnostic and seven
replay-revision unittest tests. Syntax and whitespace checks pass. No Python
files changed. The required retrospective pytest command with `-m 'not slow'`
cannot start because pytest is absent.

The configured runtime, Brain and both reports remain absent. Connected searches
did not recover the original fixed manifest or a production live report. The
retained October 18 observation linked above was re-read and still supplies only
the dated aggregate: zero live advice and two failed cases. Raw authenticated
historical receipts remain unavailable. No operational checkbox is completed.

Staging was refused on the read-only `.git/index.lock`. The source, tests and
this note are preserved in a separate local commit and transferable
`/tmp/orchestrator-live-cohort-revisions/change.bundle` and `change.patch`.
PR #1081 was observed open with `draft=false` at the base revision above.
Approval checks rejected the blocker comment and `needs-human` label because
GitHub writes require approval while this run's policy is `never`. Neither was
published; no remote checkbox was changed.
