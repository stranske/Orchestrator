# Issue #1291 repair evidence

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
