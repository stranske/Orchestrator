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

## Single-test mutation evidence — 2026-11-05 09:53Z

The mutation verifier previously accepted pytest output containing the selected
test result alongside additional passed or failed tests. It now requires exactly
one result count, matching the selected test's expected verdict. A new JavaScript
regression reproduced the false acceptance before the fix and passed afterward;
it checks both baseline and deliberate-failure output. No Python files changed.

- [x] `node --test --test-isolation=none tests/test_verify_fleet_delivery_mutation.js`:
  four tests passed, including the real reader mutation and byte-for-byte restoration.
- [x] `PYTHON=python3 node scripts/verify_fleet_delivery_mutation.js --unittest`:
  the named Python test passed, failed on the deliberate measured `0%`, and passed
  after restoration. This completes the deliberate-break check using the available
  Python 3.14.10 interpreter; it does not establish selected-interpreter acceptance.
- [x] All 25 fleet tests passed via unittest; `src/switch_review.py --selftest` passed.
- [x] The available-interpreter CLI smoke check printed all five rows at
  `2026-11-05T09:53:18+00:00` in `/tmp/orch-fleet-live-current.txt`. GitHub read errors
  and the missing opener helper were explicitly unmeasured.
- [ ] Selected-interpreter full verification and live-machine acceptance remain
  blocked: `/opt/anaconda3/bin/python3` is absent. The available interpreter lacks
  pytest, so both pytest-mode mutation verification and `src/verify.py --pytest-shard 1/6`
  exited 1. The fixture suite's unittest run is not a pytest or full-verification pass.

The requested source/test commit was attempted after `git diff --check` passed,
but Git could not create `.git/index.lock` because the Git directory is read-only.
The two JavaScript changes and this evidence update remain uncommitted.
The blocker comment and `needs-human` label were also rejected: the GitHub tools
require approval and this run's approval policy is `never`. The connector confirmed
PR #1130 remains open with `draft=false`.

## Repeatable live CLI check — 2026-11-05 10:07Z

Added `scripts/verify_fleet_delivery_live.js` and six JavaScript regression tests.
The checker runs the production `src/switch_review.py --json --text-out` command,
compares all five text rows with that JSON snapshot, requires evidence classes or
explicit read errors/missing queries, and checks that the snapshot date falls
within the current invocation. It rejects missing/duplicate rows, stale dates,
unavailable interpreters, and reused evidence directories. Runtime writes use a
private directory. A successful check writes the report and a `check.json` receipt.
The default interpreter is `/opt/anaconda3/bin/python3`; `PYTHON` selects an
explicit alternative without an automatic fallback.

Run on the target machine with a new output directory:

```sh
node scripts/verify_fleet_delivery_live.js /tmp/fleet-target-check
```

Verified in this follow-up:

- [x] All 10 JavaScript tests in the live and mutation verifier suites passed.
- [x] All 25 fleet unittest tests and `src/switch_review.py --selftest` passed.
- [x] The named fail-open test passed, failed on the deliberate `0%` mutation,
  and passed after byte-for-byte source restoration using available Python.
- [x] `PYTHON=python3 node scripts/verify_fleet_delivery_live.js
  /tmp/orch-fleet-available-1130` validated five explicitly unmeasured rows.
  Snapshot date: `2026-11-05T10:07:20+00:00`; receipt date:
  `2026-11-05T10:07:21.482Z`. This is available-interpreter runner evidence.
- [ ] Selected-interpreter live acceptance and full verification: the default
  checker failed with `ENOENT`; `/opt/anaconda3/bin/python3 src/verify.py` exited
  127 because that interpreter is absent. The available Python pytest command
  exited 1 with `No module named pytest`.

Task reconciliation: the implementation, rendering, fixture, deliberate-mutation,
and independent-row tasks are verified. The PR's checked full-verification claim
must be unchecked; selected-interpreter live acceptance must remain unchecked.
The PR-body update was attempted before new implementation work but rejected
because the GitHub tool requires approval and this run's policy is `never`.

After verification, `git diff --check` passed, but the source/test commit attempt
failed creating `.git/index.lock` on the read-only filesystem. The three scoped
files remain uncommitted. The blocker-comment and `needs-human` label attempts
were also rejected because approval is required and unavailable. The connector
reconfirmed PR #1130 is open with `draft=false` at handoff. No Python files or
collection floors changed.

## Complete label evidence — 2026-11-05 10:20Z

The first implementation task still had a measurement defect: absent or malformed
PR ownership labels and issue approval labels were silently treated as absence,
yielding undercounted numeric flow results. The extended missing-evidence regression
reproduced this before the fix. `src/switch_review.py` now requires complete label
lists for the classification each reader uses. Explicit empty lists remain valid;
closure latency does not require issue approval labels. The existing regression
now covers missing/null labels, malformed lists and entries, valid empty/nonmatching
labels, and preservation of all five rows. The suite still has 25 test methods;
no collection floor changed.

- [x] All 25 fleet tests pass under unittest; the module selftest passes.
- [x] The isolated Gate error-swallowing mutation was detected, the original source
  was restored, and the named test passed again using available Python.
- [x] All 10 JavaScript live/mutation verifier tests pass.
- [x] Available-Python CLI smoke evidence at `2026-11-05T10:20:24+00:00` retains
  all five rows, explicitly unmeasured for GitHub connectivity and the missing
  opener helper. Receipt: `/tmp/orch-672-label-evidence/check.json`.
- [ ] Required pytest, selected-interpreter full verification, and selected-interpreter
  live acceptance: pytest is missing and `/opt/anaconda3/bin/python3` is absent.
- [ ] Python commit: both the formatting command and required repository-wide Black
  check exit 127 because Black is missing. Package installation also failed.
  The pre-commit gate forbids committing these Python changes until Black passes.

`git diff --check` passes. The checklist correction, blocker comment, and
`needs-human` label were attempted but rejected because the GitHub tools require
approval and this run's approval policy is `never`. The checked pytest/full-verification
claim in the PR summary remains unsupported. PR #1130 was confirmed open with
`draft=false`. No workflow files changed.

## REST branch evidence — 2026-11-05 11:30Z

The first implementation task had another undercount: the fleet reader passes REST
PR records to backlog's source-issue rule, which expects GraphQL's `headRefName`.
Branch-only source references such as `codex/issue-1` were ignored. The reader now
adapts `head.ref` to that field and requires complete branch evidence for released
flow. The canary reader shares this validation: empty branch names previously
allowed a partial oldest age from other canaries. Latency still requires a literal
`Closes #N` body reference and is independent of branch classification.

Regression subcases reproduced both defects before the fix. All 25 fleet unittest
tests pass after the fix, including branch-only references, body/branch deduplication,
missing or malformed branch evidence, and retention of independent measurements.
No collection floor changed. The module selftest and all 10 JavaScript verifier
tests pass. The isolated Gate mutation was detected and the restored test passed
using `PYTHON=python3 node scripts/verify_fleet_delivery_mutation.js --unittest`.

`PYTHON=python3 node scripts/verify_fleet_delivery_live.js
/tmp/orch-672-branch-evidence` verified all five rows at
`2026-11-05T11:30:20.532Z`. GitHub connectivity failures and the missing opener helper
remain explicit unmeasured rows. This is available-Python runner evidence, not
selected-interpreter live-machine acceptance.

The requested pytest command and Black formatting/check commands were attempted
but cannot run because their modules are absent. Package installation failed on
PyPI DNS resolution. `/opt/anaconda3/bin/python3 src/verify.py` cannot run because
that interpreter is absent. The required Black gate prevents committing Python
changes; they remain in the working tree. Full verification and live acceptance
remain incomplete. `git diff --check` passes.

The PR checklist correction, blocker comment, and `needs-human` label were each
rejected with `MCP tool call requires approval, but approval policy is never`.
The unsupported checked pytest/full-verification claim therefore remains on the
PR. No protected files changed, and no Python commit was attempted past the
failed mandatory formatting gate.
