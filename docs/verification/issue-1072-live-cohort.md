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
