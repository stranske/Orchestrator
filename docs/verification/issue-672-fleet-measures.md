# Fleet delivery measures — 2026-11-05 runner check

Implemented `fleet_delivery_measures()` and production CLI collection in
`src/switch_review.py`. JSON and readable output share one snapshot. Each reader
fails independently; one unreadable repository prevents a partial population from
being reported as a complete measurement. PR and issue reads are cached for this
report only. The source uses backlog's agent-label and source-issue rules and
excludes Collab-Admin from the new readers.

The canary reader fetches the authoritative Workflows
[`config/consumer_sync_canaries.json`](https://github.com/stranske/Workflows/blob/main/config/consumer_sync_canaries.json).
It measures all open `sync/workflows-*` PRs, including drafts and later pages.
Latency reports the median of observed PR/issue pairs and names still-open issues
and issues closed before merge. Released flow reports 48-hour totals divided by
two days, using current approval labels.

Verified in this run:

- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_switch_review_fleet.py -q`:
  25 tests passed. These unittest tests are also collectable by pytest.
- `python3 src/switch_review.py --selftest`: passed.
- Deliberately wrapped the Gate read so an error returned a measured `0%`.
  `test_failed_gate_read_is_not_a_number` failed with `AssertionError: '0%' is not None`.
  Restored the reader and the same test passed. The mutation is absent from the source.
- CLI smoke check with writable temporary `ORCH_LOCAL_RUNTIME` and `ORCH_STATE_DIR`
  printed all five rows to `/tmp/orch-fleet-live.txt`, with explicit GitHub
  connectivity errors and an explicit missing opener-helper result. The report
  recorded `2026-11-05T00:22:36+00:00`, independently of the historical campaign target.
- `git diff --check`: passed.

Still incomplete:

- Gate REST run data identifies `pull_request`, not its `labeled`/`synchronize`
  action. The reader names the required run-ID-keyed action/conclusion join;
  it does not infer causes from actors, titles, or nearby label events.
- The opener helper is absent on this runner. Its documented three count fields
  are supported; the machine-local cap source remains unknown. A successful count
  response explicitly labels the cap unmeasured rather than assuming a limit.
- The requested `/opt/anaconda3/bin/python3` is absent and no repository-selected
  virtual environment was found. Smoke checks used the available Python 3.14.9,
  with private temporary runtime paths; this is not live-machine acceptance.
- `pytest` and `black` are absent, and shell network/DNS restrictions prevented
  installing them. Both requested commands were attempted and could not run.
  The required Black gate therefore blocks committing Python changes.
- A full `python3 src/verify.py` attempt was interrupted without a verdict.
  `python3 src/verify.py --pytest-shard 1/6` exited 1 with `No module named pytest`.
  Full verification and collection-floor reconciliation remain required. No
  collection or pass floor was changed without a measured full-suite collection.

Fixtures distinguish recorded REST/config projections and the observed shell
GitHub error from synthetic measurement examples. Offline fixtures establish
reader behavior, not production campaign measurements. No live acceptance or
full-suite task is marked complete.

## Runner follow-up — 2026-11-05

The canary reader now rejects missing or malformed paginated PR arrays. Before
the fix, four malformed responses produced an oldest age from only the remaining
canaries. The expanded existing regression covers five malformed responses and
checks that all five text rows survive while the released-flow row retains its
measured value. Valid empty populations (`[[]]`) still report measured absence.
The fleet suite still contains 25 test methods; no collection floor was changed.

- [x] All 25 fleet tests pass via unittest; the module selftest passes.
- [x] The deliberate Gate error-swallowing mutation returns `0%` and makes
  `test_failed_gate_read_is_not_a_number` fail; restoring the source makes it pass.
- [x] Malformed-reader and failed-read text-output tests retain all five rows.
- [ ] Required pytest, Black, full verification, and live-machine acceptance.

The runner CLI smoke check at `2026-11-05T06:08:10+00:00` printed all five rows,
explicitly unmeasured for unavailable GitHub/helper reads. The selected Anaconda
interpreter and a selected virtual environment remain absent. Pytest and Black
are unavailable, and installation failed on PyPI DNS; Python changes remain
uncommitted because the mandatory Black gate cannot run. The full available-Python
verification attempt was interrupted without a verdict. GitHub rejected both the
blocker comment and `needs-human` label because tool approval is required but the
run's approval policy is `never`. PR #1130 was confirmed open with `draft=false`.

## Repeatable mutation control — 2026-11-05 08:35Z

`scripts/verify_fleet_delivery_mutation.js` now copies the reader and fixtures to a
private temporary tree, runs the named fail-open test, wraps the Gate reader to
swallow its error and return `0%`, requires that exact assertion failure, restores
the original source bytes, and requires the test to pass again. It refuses to
count missing tools, skipped tests, unrelated failures, or timeouts as evidence.
Cleanup and restoration also run when verification fails. The checkout is never
mutated. The interpreter can be selected with `PYTHON`; otherwise the verifier
requires the issue's `/opt/anaconda3/bin/python3`.

- [x] `PYTHON=python3 node scripts/verify_fleet_delivery_mutation.js --unittest`: baseline passed,
  mutation detected, restored test passed using available Python 3.14.9.
- [x] `node --test --test-isolation=none tests/test_verify_fleet_delivery_mutation.js`:
  all three tests passed, including the real mutation and failed-run restoration.
- [x] All 25 existing fleet tests pass via unittest; the module selftest passes.
- [x] The runner CLI printed all five rows at `2026-11-05T08:35:38+00:00`, with
  explicit GitHub connectivity and missing-helper results. This is a runner smoke
  check, not the selected-interpreter live-machine acceptance.
- [ ] `PYTHON=python3 node scripts/verify_fleet_delivery_mutation.js` (pytest mode):
  blocked by missing pytest, correctly reported as a failure.
- [ ] Required pytest, Black, selected-interpreter full verification, and
  live-machine acceptance remain unavailable. PyPI DNS resolution failed when
  attempting to install the missing tools. `src/verify.py --pytest-shard 1/6`
  exited 1 because pytest is missing; the full verification attempt was interrupted
  without a verdict.

Only JavaScript and this evidence note changed in this follow-up. No Python
source or formatting configuration changed, and no collection floor was adjusted.

The requested source commit was attempted but Git could not create
`.git/index.lock`: the Git directory is read-only in this run. The new JavaScript
files and this note remain in the working tree. Attempts to post the blocker
comment and add `needs-human` to PR #1130 were also rejected because tool approval
is required and this run's approval policy is `never`. The connector confirmed
PR #1130 is open with `draft=false`.
