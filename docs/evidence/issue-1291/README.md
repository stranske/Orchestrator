# Issue #1291 repair evidence

## Round 3 live readback — 2026-11-09

Outcome 1: PASS for the measured-toolchain gate on input branch head
`feda5700fc304a420708f7fda1d88721d9955fc9`. Before any change this round,
`/opt/anaconda3/bin/python3 -m pytest -q tests/test_ci_gate_config.py`
passed all 15 tests in 0.39 seconds. The existing regression exercises every
one of the nine tools, including bumped-pin rejection, missing-entry rejection
with the diagnostic, and complete matching acceptance after fixture restoration.
Stop rule: three failed repair attempts or 45 minutes. No repair attempt was
needed: the reported jobs do not currently demonstrate a code failure.

`gh run view 42581101602 --json conclusion,headSha,jobs` reports the named
Resolve Context and Record autofix dispatch completion jobs **cancelled** on
that exact head, rather than failed. `gh run view 42581101602 --log-failed`
exits zero without failed logs. Health 45 run `42581101070` is successful;
the later status context points to successful run `42581103989`. PR #1293
is open, ready for review, and has no failing check contexts at this readback.
No shared workflow modification is justified by these results.

Source CI run [42580639059](https://github.com/stranske/Orchestrator/actions/runs/42580639059)
has head SHA `feda5700fc304a420708f7fda1d88721d9955fc9` and concludes success.
Both checkout and flat-copy aggregates succeed. The flat aggregate job
`129043240470`, read with
`gh run view 42580639059 --job 129043240470 --log`, runs the existing
`bash scripts/verify_before_sync.sh "$GITHUB_WORKSPACE"` invocation with
`VERIFY_BEFORE_SYNC_AGGREGATE=.../mirror-evidence` and payload integrity checks.
Its checkout is the PR merge revision `8d8f2d9`, not the branch revision itself.
It reports BARE EXEC MIRROR, 15,068 collected, 15,014 passed, zero failed,
52 named test skips, and the terminal `verify-before-sync: VERIFIED` result.
The seven named selftest skips and two named gate skips remain visible in the
native log; this is not a claim that every capability gate ran.

Local committed-input shipping was independently checked with:

```bash
bash scripts/build_exec_mirror.sh "$PWD" /private/tmp/1293-round3-flat
cmp docs/CI_LINT_BASELINE.md /private/tmp/1293-round3-flat/docs/CI_LINT_BASELINE.md
cmp .github/workflows/autofix-versions.env /private/tmp/1293-round3-flat/.github/workflows/autofix-versions.env
cd /private/tmp/1293-round3-flat
/opt/anaconda3/bin/python3 -m pytest -q tests/test_ci_gate_config.py
```

Builder and both comparisons exit zero; the flat gate file passes all 15 tests
in 0.33 seconds. These checks use unchanged branch pins. The proposed #1289
payload has the separate strict measurement and shipping receipts below.
No dependency pins, measurement markers, thresholds, or implementation change
in this round. No full test suite was run locally.

Delivery remains pending: #1289 is open and ready at unchanged head
`a27084d488d77fcfe7c9644ab97c8f5426bf5cfc`. Its four failed shard/aggregate
jobs still refer to historical run `42567594229`; that run is not new delivery
evidence. No subsequent sync-core classification is recorded. T7/A5/A6 remain
pending until source delivery and native regeneration. This evidence-only
commit also requires its own fresh CI readback; the successful source aggregate
above does not establish aggregate acceptance for a future commit or #1289.

Check date: 2026-11-09. Input source: Orchestrator
`d2fdd91b4b09f2a9a8572e14cb5270c3c83628d0`; proposed pin bytes read from
PR #1289 head `a27084d488d77fcfe7c9644ab97c8f5426bf5cfc` through the GitHub
contents API into a disposable clone. No repository dependency pins are edited.

Outcome 1 required: the measured-toolchain gate passes for that complete proposed
bundle after actual strict measurement. Exact focused reproduction:

```
cd /private/tmp/1291-payload
/opt/anaconda3/bin/python3 -m pytest -q tests/test_ci_gate_config.py::test_baseline_was_measured_with_the_pinned_versions
```

Before implementation this failed in 0.42 seconds with `CI_LINT_BASELINE.md has
no complete measured toolchain matching autofix-versions.env`. The same command
passes against the branch's unchanged current pins. Stop rule: three repair
attempts or 30 minutes, whichever comes first; stop at the first broken step.

## T1: all pins versus the latest complete measurement

| Tool | Branch and measured 2026-10-22 | Proposed #1289 | Disposition |
| --- | --- | --- | --- |
| BLACK | 26.10.1 | 26.10.1 | matches |
| RUFF | 0.17.2 | 0.17.4 | no matching measurement |
| ISORT | 9.0.2 | 9.0.2 | matches |
| DOCFORMATTER | 1.7.8 | 1.7.8 | matches |
| MYPY | 2.4.0 | 2.4.1 | no matching measurement |
| PYTEST | 9.1.1 | 9.2.0 | no matching measurement |
| PYTEST_COV | 7.1.0 | 7.1.0 | matches |
| PYTEST_XDIST | 3.8.0 | 3.8.0 | matches |
| COVERAGE | 7.17.0 | 7.17.2 | no matching measurement |

No entry is missing from the existing complete marker; the proposed bundle has
four mismatches. Older incomplete historical markers cannot satisfy the gate.

## T2: source ownership and shipping shape

Inspected Workflows revision `d17b088aa5c97e6b024101c39252480c622e8395`.
`maint-68-sync-consumer-repos.yml` uses `.github/sync-manifest.yml` for shared
consumer infrastructure. The dev-tool payload specifically comes from
`maint-52-sync-dev-versions.yml`, which copies Workflows'
`.github/workflows/autofix-versions.env` into the consumer. Its source-pin writer
is `maint-auto-update-pypi-versions.yml`. Baseline measurements and collector are
Orchestrator-owned: `docs/CI_LINT_BASELINE.md` and `scripts/ci_lint_baseline.py`.
Changing a shared template would not supply these repository-specific measurements.

`scripts/build_exec_mirror.sh SRC MIRROR` constructs the flat copy. Its `.github/`,
`docs/`, `scripts/`, and tests come from git HEAD; modules come from the working
tree. Thus baseline and pins must be committed in the input checkout before the
builder can ship them. The CI aggregate calls `scripts/verify_before_sync.sh`
with `ORCH_SYNC_SCRIPT=scripts/build_exec_mirror.sh`,
`VERIFY_BEFORE_SYNC_TREE='BARE EXEC MIRROR'`,
`VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG=1` and
`VERIFY_BEFORE_SYNC_AGGREGATE=<mirror-evidence>`; the wrapper hashes the payload
before and after verification. Aggregate requires genuine six-shard evidence;
focused tests are not a substitute.

## Round-2 reported failures

The model-table drift failure reproduced in 0.60 seconds with:
`/opt/anaconda3/bin/python3 -m pytest -q tests/test_model_table.py::test_copy_matches_workflows_source`.
Both checkout and mirror shard 6 logs identify that assertion. The authoritative
copy is Workflows `.github/agents/model-table.json`, read at the exact revision
above. The documented `src/model_table.py --sync` command is the repair.
Resolve Context job 129013169661 and dispatch-record job 129013175310 read back
as cancelled with no steps, not application failures. Other cancelled jobs and
new-head CI remain to be read back; no workflow repair is inferred from missing logs.

## Verified measurement and regression

`/private/tmp/1291-toolchain/bin/python` is an explicitly selected venv created
by `/opt/anaconda3/bin/python3 -m venv`; all nine exact proposed versions were
installed along with the unchanged `requirements.lock` runtime requirements.
The collector's `collect(strict=True)` entry point ran with that interpreter
and its bin directory first on PATH. [report.json](report.json) records every
actual version and all blocking/drainable pairs. [commands.json.gz](commands.json.gz)
is the complete captured command/output stream (gzip-compressed JSON, no excerpts).
[provenance.json](provenance.json) binds the input source, proposed payload,
interpreter, collector digest and pin digest. The source clone contains only
the authenticated proposed pin payload as its measurement overlay.

All lint commands exited zero: Ruff 0 findings; Black 1,014 unchanged files;
mypy no issues in 229 source files. Coverage startup/collection exited zero,
collecting 15,067 tests in 54.49 seconds. No full test suite was run locally.
These counts do not require a gate/floor change. The baseline now records this
complete bundle in addition to the previously measured unchanged branch bundle.

The existing three parametrized regression cases now exercise the public gate
and its diagnostic for missing entries and mismatched/mixed versions, then
accept a complete matching bundle, reject a bumped fixture pin and accept its
revert. No additional collected test was added. A deliberate unhandled fixture
Ruff bump at the acceptance call failed all three cases with `has no complete
measured toolchain matching autofix-versions.env`:
[deliberate break](deliberate-break.txt.gz). Reverting the fixture restored
[15 passing gate tests in 0.30 seconds](revert-pass.txt).

The proposed-payload gate file also passed all 15 tests after the baseline
update. Both current and proposed bundles retain complete, separate provenance.

## Pending live acceptance

PR #1289 remains at `a27084d488d77fcfe7c9644ab97c8f5426bf5cfc` before source
delivery. T7/A5/A6 require a regenerated head, a new CI run, and a subsequent
sync-core classification. They are pending; this repair branch cannot claim them
from local tests or a historical run. The four additional reported round-2 jobs
(Resolve Context 129013169661/129015952995 and Record autofix dispatch completion
129013175310/129016130909) all read back as cancelled with no steps.

## Keepalive integration

Before push, fetch found completed hosted commit
`6c8b06a7f50c0a092f27367b8ad5179b74d0d7b7`. Its fixture-file regression was
retained instead of duplicating it. Its historical
`docs/verification/issue-1291-pin-conflict.md` records that hosted runner's lack
of tooling; the successful local measurement above supersedes that environment
blocker. Running its tests exposed one exact diagnostic-comparison failure:
pytest appends assertion introspection to the message. The comparison now checks
the full expected diagnostic prefix, including pins and repair instruction,
while allowing pytest's appended explanation. The same deliberate-break/revert
exercise was repeated on this retained implementation; the linked receipts are
from that final implementation. The combined gate/model-table run passes 38 tests.

## Flat-copy verification and remaining aggregate evidence

Built a disposable checkout of the repaired branch, then overlaid the exact
GitHub-read #1289 pin payload and committed that fixture locally (never pushed).
The fixture commit was `06194a20`; its only extra change is the authenticated
four-version payload. Commands executed:

```
bash scripts/build_exec_mirror.sh /private/tmp/1291-final-payload /private/tmp/1291-flat
cmp /private/tmp/1291-final-payload/docs/CI_LINT_BASELINE.md /private/tmp/1291-flat/docs/CI_LINT_BASELINE.md
cmp /private/tmp/1291-proposed-pins.env /private/tmp/1291-flat/.github/workflows/autofix-versions.env
cd /private/tmp/1291-flat
/opt/anaconda3/bin/python3 -m pytest -q tests/test_ci_gate_config.py tests/test_model_table.py
```

Builder exited zero and shipped 229 flat modules and 678 top-level test files.
Both byte comparisons exited zero. The focused flat-copy command passed all
38 tests in 0.55 seconds. This proves the baseline/pin payload and focused gate
in the intended shape; it does **not** prove T6/A4's six-shard aggregate.

New source CI run `42575686151` was queued/pending after push of `fa75d723`.
No fresh six-shard aggregate artifacts were available at readback. The exact
payload-integrity aggregate command in T2 remains to be executed with those
fresh artifacts; old failed-head artifacts are not substituted or rewritten.
No local full suite was run to manufacture aggregate evidence. T6/A4 remain
pending alongside T7/A5/A6. No review or inline comments were present at the
initial post-push read. The automation's next scheduled pass owns the seven-minute
review-floor readback, new CI/aggregate inspection, and subsequent delivery checks.

Verified locally: T1, T2, T3, T4, T5, A1, A2, A3. Live delivery and aggregate
criteria are explicitly pending. This evidence update changes documentation only.

## Hosted follow-up: committed toolchain payload regression

Checked 2026-11-09 against source
`db4c1d3e45988d4db8985212f77f42eee6788273`. Re-read the Workflows source through
the GitHub connector before editing. Exact Git blob identities:

| Workflows input | Git blob |
| --- | --- |
| `.github/workflows/maint-68-sync-consumer-repos.yml` | `080f01d98ad21a02a372bd851e9a4641b5851f50` |
| `.github/workflows/maint-52-sync-dev-versions.yml` | `8e03fa90fcf59516739cd12160b297122caa9a19` |
| `.github/sync-manifest.yml` | `8a6aa86bc4ad22c0a7f88c280feb86365fff287d` |
| `docs/ci/WORKFLOWS.md` | `7578b2d4c55ff3099bf5054d7c6550c7213246e5` |

- [x] T2: inspected the sync lanes and local construction/aggregate commands;
  verified the authoritative source of both payload files.
- [ ] T6/A4: six-shard aggregate acceptance remains pending. The automated PR
  summary incorrectly marks T6 complete; the earlier flat-copy focused run
  does not establish aggregate success.
- [ ] T7/A5/A6: regenerated sync head and subsequent sync-core acceptance remain
  pending. PR #1289 still reads as open, ready, at
  `a27084d488d77fcfe7c9644ab97c8f5426bf5cfc`.

Maint 68 is absent from this consumer checkout; its authoritative Workflows
implementation consumes the sync manifest. That manifest excludes the consumer
pin file from copy-sync, while Maint 52 explicitly copies the canonical pins
into consumers. `docs/CI_LINT_BASELINE.md`, the collector, the mirror builder,
and the new regression are Orchestrator-owned. No workflow changes are needed.

`tests/test_ci_toolchain_mirror.js` now exercises the actual builder in a
disposable git repository using the real committed baseline/pin bytes. It
compares both mirror files byte-for-byte with the fixture's HEAD, verifies that
an uncommitted baseline repair is not shipped, then commits that fixture repair
and verifies that rebuilding refreshes the baseline while retaining the pins.
The fixture's pin file is copied unchanged; no consumer pin file is edited.

Validation:

```
node --test --test-isolation=none tests/test_ci_toolchain_mirror.js
node --check tests/test_ci_toolchain_mirror.js
git diff --check
```

All three commands exit zero; the new test passes in 0.25 seconds. The existing
Node mirror suite was also run before and after the addition. The combined
command `node --test --test-isolation=none tests/test_exec_mirror_contract_witness.js tests/test_ci_toolchain_mirror.js`
reports six passing tests and one pre-existing failure in
`scratch installer publishes every builder byte and mode and preserves runtime output`:
`tools changed bytes or mode during installation` (expected empty directory,
actual absent). That separate installer issue is not changed here.

This hosted runner has no pytest or Black. Installing them into a temporary
directory failed with `No matching distribution found for black`. No Python
source was changed in this follow-up; the earlier measured Python results
remain the evidence for T3-T5/A1-A3, rather than a claimed hosted rerun.

Source CI run `42575806594` was still in progress at readback: flat shards 2-6
and checkout shards 2/4/5/6 had succeeded; the remaining shards and aggregates
were pending. These are source-branch results, not regenerated #1289 evidence.
PR #1293 reads as open with `draft=false`. Updating its checklist through the
connector was rejected with `MCP tool call requires approval, but approval policy
is never`; the verified T2 completion and T6 correction are recorded here.

The required source commit was attempted after the checks above. `git add`
failed with `Unable to create .../.git/index.lock: Read-only file system`, so
`git commit` did not run. The JavaScript regression and this follow-up evidence
remain uncommitted; this run cannot claim the round's commit requirement met.

## Hosted follow-up: baseline integrity through publication

Checked 2026-11-09 against source
`49f6d4e175923a3736dfca67dbccf3a0e91feb5e`. The first unchecked task, T7,
still depends on source delivery and regeneration: #1289 remains open, ready,
at `a27084d488d77fcfe7c9644ab97c8f5426bf5cfc`, with no new sync-core
classification in its comments. T6/A4 also remain incomplete.

Extended the existing `tests/test_ci_toolchain_mirror.js` regression to exercise
the shipped installer and digest implementation. Uncommitted baseline edits
leave the mirror digest unchanged; committing and rebuilding changes it.
Changing the shipped baseline after capturing that digest rejects publication
with `retained snapshot digest ... != verified payload ...` and preserves the
destination. Restoring the baseline restores the digest and permits publication
of both baseline and pins byte-for-byte. The scratch installation explicitly
uses `--unverified`: this tests integrity, without claiming an aggregate verdict.
No workflow or pin file was edited, and no collected test was added.

The focused Node command passes in 1.03 seconds:
`node --test --test-isolation=none tests/test_ci_toolchain_mirror.js`.
`node --check tests/test_ci_toolchain_mirror.js` and `git diff --check` pass.
`python3 -m pytest -q tests/test_ci_gate_config.py -m 'not slow'` still exits
before collection with `No module named pytest`; no Python source was changed.

Read the actual source CI run
[`42575806594`](https://github.com/stranske/Orchestrator/actions/runs/42575806594),
whose artifacts identify input head `db4c1d3e45988d4db8985212f77f42eee6788273`:

| Previously failed job | Current source-run result |
| --- | --- |
| pytest + coverage shard 1/6 | success, job 129023369879 |
| flat exec-mirror shard 1/6 | cancelled, job 129023369891; upload failed |
| verify.py aggregate + combined coverage | success, job 129026417491 |
| verify.py aggregate in the flat exec-mirror shape | failure, job 129026456227 |

The flat aggregate log names the first broken step:
`aggregate FAILED: missing or unreadable summary for shard 1/6`, because
`mirror-evidence/shard-1.json` is absent. The artifact list independently has
mirror shards 2-6 and no shard 1. This is not evidence of a new measured-toolchain
failure and does not authorize manufacturing the missing shard result.

The newer source CI run
[`42576841952`](https://github.com/stranske/Orchestrator/actions/runs/42576841952)
is still running for input head `49f6d4e175923a3736dfca67dbccf3a0e91feb5e`.
Neither source run establishes regenerated-#1289 acceptance. PR #1293 was
re-read as open with `draft=false`; no completion checkbox is changed here.

The combined mirror command was also executed, without skipping any existing
test: `node --test --test-isolation=none tests/test_exec_mirror_contract_witness.js tests/test_ci_toolchain_mirror.js`.
It reports six passes and the previously documented failure at
`tests/test_exec_mirror_contract_witness.js:153`: the installer omits an empty
`tools` directory. That unrelated failure is unchanged.

After validation, `git add tests/test_ci_toolchain_mirror.js docs/evidence/issue-1291/README.md`
failed with `Unable to create .../.git/index.lock: Read-only file system`.
The commit could not proceed. This follow-up's test and evidence edits remain
in the working tree; the round's mandatory source-commit requirement is unmet.

## Hosted follow-up: real aggregate rejection in the flat payload

Checked 2026-11-09 against source
`ec8053d7f7a04f42a6c273247aeedff1b3a54fc0`. T7 remains blocked on source
delivery and regeneration: #1289 still has head
`a27084d488d77fcfe7c9644ab97c8f5426bf5cfc`, and its comments contain no new
sync-core classification. No workflow or protected pin file was changed.

Continued T6's regression in the existing Node test. The fixture now ships the
real committed Python modules and shard plan through the builder, then executes
`python3 verify.py --floor-may-lag --aggregate <empty-evidence-directory>`
inside that flat copy. It asserts exit 1 and the exact missing-shard diagnostic,
including the shard count from the shipped plan and the missing file path.
Both toolchain files must remain byte-identical to the fixture commit, and the
entire payload digest must remain unchanged after rejection. Inherited runtime
overrides are removed and all state paths point inside the disposable fixture.
This tests the actual aggregate entrypoint without pytest substitutes or
manufactured shard results. It does not establish a successful aggregate.

Validation:

- `node --test --test-isolation=none tests/test_ci_toolchain_mirror.js`: one
  passing test, zero skips, in 2.76 seconds.
- `node --check tests/test_ci_toolchain_mirror.js` and `git diff --check`: pass.
- The combined mirror command documented above: six passes and the same
  pre-existing empty-`tools` directory failure at
  `tests/test_exec_mirror_contract_witness.js:153`. No test was skipped or disabled.
- No Python source was edited; this interpreter still has no pytest, so no
  Python suite or new baseline measurement is claimed.

Source CI run
[`42576841952`](https://github.com/stranske/Orchestrator/actions/runs/42576841952)
has now completed. Its checkout aggregate failed (job `129028767571`), and its
flat aggregate failed (job `129028776873`). The latter log explicitly reports
`aggregate FAILED: missing or unreadable summary for shard 1/6` and
`NOT VERIFIED (verify.py exit 1)`. Its artifacts contain only mirror shards
2/3/4/6; shards 1 and 5 were cancelled. Checkout shard 1 was also cancelled.
These results cannot establish T6/A4 or regenerated-head acceptance.

The current source-head CI run
[`42577582423`](https://github.com/stranske/Orchestrator/actions/runs/42577582423)
is still in progress at readback, with checkout shards 2/4 and mirror shards 2/4
successful and the remaining shards running. #1293 and #1289 both read as open,
ready pull requests. T6/A4 and T7/A5/A6 remain pending; no checkbox is completed
by this follow-up.

After validation, the required commit was attempted with the two changed files.
`git add` failed with `Unable to create .../.git/index.lock: Read-only file system`;
the commit command therefore did not run. The source/test and evidence changes
remain uncommitted. A writable Git index is required to satisfy the round's
mandatory source-commit requirement.

Attempting to post this blocker on #1293 and add `needs-human` was also rejected:
both connector mutations returned `MCP tool call requires approval, but approval
policy is never`. Neither the comment nor the label was added; this local
evidence remains the handoff record.

## Hosted follow-up: aggregate wrapper refuses snapshot publication

Checked 2026-11-09 against source
`2c15591040c72c59308d0985ab47f2269bffd85d`. The first unchecked task, T7,
still awaits source delivery and sync regeneration: #1289 remains open and
ready at `a27084d488d77fcfe7c9644ab97c8f5426bf5cfc`, with no subsequent
sync-core classification in its comments. T6/A4 remain pending despite the
automated task summary's checked T6 box.

Extended the existing Node regression for T6 to execute the committed
`scripts/verify_before_sync.sh` with the real builder, Python modules and shard
plan. The invocation uses the CI aggregate settings, disposable runtime/state
paths and an empty evidence directory, and requests both a snapshot and a
digest receipt. It must reach the real missing-shard diagnostic, retain
`NOT VERIFIED (verify.py exit 1)`, and publish neither output. The original
mirror's baseline, pins and complete payload digest remain unchanged. The
storage supervisor remains active; its documented refusal exit 78 is accepted
only alongside its refusal diagnostic and the actual failed aggregate verdict.
This runner refuses open-file inspection at retirement, after verification.
No successful aggregate or manufactured shard result is claimed.

Validation:

```
node --test --test-isolation=none tests/test_ci_toolchain_mirror.js
node --check tests/test_ci_toolchain_mirror.js
git diff --check
node --test --test-isolation=none tests/test_exec_mirror_contract_witness.js tests/test_ci_toolchain_mirror.js
```

The focused regression passes in 4.96 seconds; syntax and whitespace checks
pass. The combined command reports six passes, zero skips and the previously
documented failure at `tests/test_exec_mirror_contract_witness.js:153`:
`tools changed bytes or mode during installation`. No Python source changed.
`python3 -m pytest --version` reports `No module named pytest`, so no Python
rerun or baseline measurement is claimed.

At 07:28 UTC, source CI run
[`42578361817`](https://github.com/stranske/Orchestrator/actions/runs/42578361817)
has all six checkout shards and flat shards 2-6 successful; flat shard 1 and
checkout aggregate are still running. The flat aggregate has not started.
`gh run view 42578361817 --repo stranske/Orchestrator --log-failed` cannot
connect to `api.github.com` in this runner; these job results were read through
the GitHub connector. Neither source CI nor this negative regression establishes
regenerated-#1289 acceptance. No task completion checkbox is changed.

After validation, `git add tests/test_ci_toolchain_mirror.js` fails with
`Unable to create .../.git/index.lock: Read-only file system`, preventing the
mandatory source commit. Posting a blocker comment and adding `needs-human`
to #1293 both return `MCP tool call requires approval, but approval policy is
never`. Neither mutation occurred. The tested source/test change and this
evidence remain uncommitted and require a writable Git index for delivery.
