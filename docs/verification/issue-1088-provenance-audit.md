# Issue #1088: source binding audit and remaining evidence limits

Observation date: 2026-11-03, during this hosted runner session. Issue acceptance
remains incomplete. This observation is not an October 20 replay receipt and
does not establish an eligible production candidate, apply, or resolved stall.

## Historical evidence search

The [original issue discussion](https://github.com/stranske/Orchestrator/issues/582#issuecomment-6167058330)
reports a native 12/12 provenance audit and a later exact-merge mutation recheck.
The [PR #595 provenance comment](https://github.com/stranske/Orchestrator/pull/595#issuecomment-6166759133)
names the private binding receipt:

`/Users/teacher/.codex/automations/pd-workloop-resume/coordination-20261014T0548/independent-582-provenance.json`

The issue discussion also names:

`/Users/teacher/.codex/automations/imi-merge-verify-closer/work/20261014T1047Z/582-deliberate-recheck.json`

Neither file nor `~/.codex/orchestrator/redirect-shadow/shadow.jsonl` is available
in this runner. The committed issue-582 verification directories and the fetched
issue #582 / PR #595 discussions contain earlier replay and mutation evidence,
but no genuine October 20 execution receipt was recovered. The comments establish
that an owner reported a private audit; they do not supply independently
inspectable source-to-fixture bindings here. Historical provenance remains
unverified in this session. No source identities were reconstructed from fixtures.

## Read-only audit implementation

Run this on the owner's host with the original corpus or its retained snapshot:

```sh
node scripts/audit_redirect_stall_provenance.js /path/to/private/shadow.jsonl
```

An optional second argument selects the fixture directory from the revision being
audited. The auditor requires the exact twelve-file roster, matches unique source
reports by retained sweep fingerprint, compares every retained report fact, checks
the added policy mapping, and requires a one-to-one target anonymization. Duplicate,
missing, reused, changed, or inconsistently anonymized bindings fail. Additional
stalled source reports are counted but cannot substitute for a fixture's binding.

Output binds each fixture byte hash to a source line and entry hash under the
observed corpus byte hash. It omits raw source targets, report contents, and entry
identities. It never writes source, fixtures, state, or a receipt file. Success
means the supplied source and fixtures match; authenticating the supplied corpus
and comparing its hash with an original owner receipt remain owner responsibilities.

Every result uses the current observation timestamp and explicitly retains
`historical_replay_receipt: not_established`. It does not execute a replay or a
live action. A missing source exits 1 with `status: unavailable` and
`private_source_corpus_unavailable`, rather than inferring an empty candidate
population. This is the actual result against the expected corpus location here.

## Verification in this session

```sh
node --test --test-isolation=none tests/test_redirect_stall_provenance.js tests/test_redirect_stall_fixtures.js tests/test_redirect_sweep_shell_backend.js
```

16 passed, zero failures or skips: seven new auditor tests plus the nine existing
offline replay/sweep checks. Auditor tests use explicitly manufactured temporary
source inputs; those inputs are only controls for the implementation and establish
no historical provenance. They cover retained-fact mutations, both directions of
target anonymization, missing/duplicate/reused bindings, malformed input, missing
fixtures, source immutability, sanitized output, and CLI exit behavior. CLI tests
call the real entry point in-process because this runner restricts spawning Node
children. Direct shell invocation also confirmed the unavailable-source exit 1.
Both JavaScript syntax checks and `git diff --check` pass. No Python files changed.

Separately, a temporary `git archive` of merged revision
`8f596dc01e1ede350724c1b88bc36610380af79b` was evaluated with the repository-selected
Python 3.14 interpreter, `/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3`:

| Execution | Exit | Result |
| --- | --- | --- |
| Both required named pytest nodes, `-q -m "not slow"` | 1 | `No module named pytest`, before collection |
| Both focused pytest files, `-q -m "not slow"` | 1 | `No module named pytest`, before collection |
| `src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls` | 0 | `authorized 12/12 with named agents (dry-run)` |

The nodes are
`tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine`
and
`tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply`.
The focused files are `tests/test_redirect_chain_gate.py` and
`tests/test_redirect_apply_prescreen.py`. Installing pytest failed because the
package index hostname could not resolve; `/opt/anaconda3/bin/python3` is absent.
These results do not satisfy the focused-execution criterion. The temporary
archive was removed after evaluation.

## Outstanding acceptance

The first task remains open for recovery or owner qualification of the private
bindings and any historical October 20 receipt. The second remains open until
the required pytest execution succeeds on an identified merged revision.

This runner has no installed production state or owner cadence receipts. Armed
flag state, eligible candidates, refusal records, blocking/drainable counts, and
action lineage are unmeasured here; none is reported as zero. Demand counts and
the fixture population cannot establish production eligibility. The third and
fourth tasks require the owner host's actual observation and the existing bounded
cadence. No flag, daily cap, writer lock, routing threshold, or cadence was changed.
No live outcome or control comparison is claimed, and all operational acceptance
remains incomplete.

## Keepalive reconciliation and exact-revision verifier

Reviewed the bootstrap commit `8b844a0` and implementation commit `1df14c5` before
continuing. The latter changed three files: the auditor, its tests, and this
report. Re-running its sixteen Node checks passed without failures or skips.
The private-source recovery task remains blocked; the explicitly retained
historical-provenance limitation is verified. A PR-body reconciliation was
attempted through the GitHub connector, but it returned
`MCP tool call requires approval, but approval policy is never`. No remote
checkbox update succeeded. The following is the local reconciliation of the
five distinct tasks and five acceptance criteria (duplicates have the same state):

- [ ] Recover private bindings and an October 20 receipt: private files unavailable.
- [ ] Reproduce named tests, focused files, and replay: pytest unavailable.
- [ ] Establish November 3 population, refusals, flag, cadence, and lineage: owner state unavailable.
- [ ] Observe an attributed bounded real apply, resolution, and control: not established.
- [x] Keep operational acceptance incomplete and distinguish historical dates from current observation.
- [ ] Exact-revision focused execution succeeds and replay authorizes at least 9/12.
- [x] Retain the historical-provenance limitation explicitly.
- [ ] Document actual November 3 eligible candidates and refusals.
- [ ] Establish an attributable apply, acted_on > 0, resolution timing, and control.
- [x] Do not represent a later run as historical execution.

`scripts/verify_redirect_merged_revision.js` now provides one offline command
for the second task:

```sh
node scripts/verify_redirect_merged_revision.js 8f596dc01e1ede350724c1b88bc36610380af79b /opt/anaconda3/bin/python3
```

Use the repository-selected Python interpreter when the owner interpreter above
is unavailable. The command requires a full commit SHA already reachable from
the local `origin/main` reference and records that reference's SHA. It archives
the revision's complete `src/` and `tests/` trees plus `pyproject.toml`, hashes
the archive, and runs both named nodes, both focused files, and the public offline
replay from the extracted bytes. It does not execute dirty working-tree files.
Each Python command gets private temporary operational state, the live flag and
heartbeats off, and no inherited Python source path or pytest options. The
temporary tree is removed even on execution failure. It does not install pytest
or substitute another test runner.

The JSON result records the actual observation time, interpreter identity,
commands, exit statuses, process errors, stdout, and stderr. Success requires
executed, passing tests without skips and `authorized N/12` with 9 <= N <= 12;
a zero exit with a process error is also blocked. Historical replay receipt and
operational acceptance remain `not_established` even on an offline success.

The actual new execution is retained in
[`issue-1088-merged-revision.json`](issue-1088-merged-revision.json). It used
`/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3` on merged revision
`8f596dc01e1ede350724c1b88bc36610380af79b`, observed at
`2026-11-03T15:59:31.876Z`. Both pytest invocations exited 1 before collection
with `No module named pytest`. Offline replay exited 0 and printed
`authorized 12/12 with named agents (dry-run)`. The subprocess layer additionally
reported `EPERM`, which the verifier retains rather than silently ignoring.
Installing pytest and Black into a temporary dependency directory also failed
with no available distribution. The aggregate result is **blocked**, not verified.
This is a new runner observation, not an October 20 receipt or an observation
of the November 3 production chain.

Six new verifier tests use explicitly manufactured Git repositories and Python
process results; they establish no historical execution. They exercise dirty
source isolation, actual Git ancestry and archive extraction, unmerged-revision
refusal, missing pytest, skipped/empty execution, replay totals, process failures,
and cleanup. The combined command passes all 22 checks without skips:

```sh
node --test --test-isolation=none tests/test_verify_redirect_merged_revision.js tests/test_redirect_stall_provenance.js tests/test_redirect_stall_fixtures.js tests/test_redirect_sweep_shell_backend.js
```

Both new JavaScript files pass `node --check`; `git diff --check` passes. No
Python or workflow files changed. Production blocking count: **unknown**;
drainable count: **unknown**; eligible candidates: **unknown**, not candidates 0.
Neither the missing owner runtime nor an offline fixture count establishes an
empty production population. No observed production failure was repaired and no
bounded cadence, writer lock, or daily cap was changed.

The required source commit was attempted after validation, but `git add` failed:
`.git/index.lock: Read-only file system`. No files were staged or committed by
this iteration. PR #1089 was re-read afterward and remains open with
`draft=false` at `1df14c5c85945066c567f733b61107f3177bd888`.

## Third-iteration observation and auditor correction

At `2026-11-03T16:17:34Z`, this runner still had no
`/home/runner/.codex/orchestrator` directory or `/Users/teacher/.codex/orchestrator`.
The default corpus, stage-2 plan, sweep report, `.last-redirect-apply-link`,
`redirect-apply.log`, and `switch-review.json` were absent. None of
`ORCH_LOCAL_RUNTIME`, `ORCH_STATE_DIR`, `ORCH_REDIRECT_SHADOW_CORPUS`, or
`ORCH_REDIRECT_APPLY_BOOTSTRAP` was set in the runner environment. This establishes
an unavailable owner-runtime observation, not an empty production population.

Production blocking count: **unknown**; drainable count: **unknown**; eligible
candidates: **unknown**. Refusals, action lineage, and actual cadence execution
are also unavailable. The checkout exports the bootstrap default as 1 and declares
the existing bounded apply/link cadence with GitHub preflight deferral. Source
configuration does not establish the deployed November 3 flag or successful
execution. No production break can be located from these missing artifacts;
demand 18 supplies no eligibility evidence. No live action was attempted here.

Reviewed `1df14c5` and `ba18153` and reran all 22 existing scoped Node checks
successfully before reconciling the checklist. The three completed distinct
items remain exactly those checked in the local reconciliation above (six
checkboxes across the PR body's duplicated sections). The attempted PR-body
update was rejected with `MCP tool call requires approval, but approval policy
is never`; the remote checkboxes were not updated. Pytest remains unavailable
under the repository-selected interpreter. Private bindings and a genuine
October 20 receipt remain unavailable; the first four distinct tasks stay open.

A manufactured corpus whose targets exactly equal the fixture targets exposed
a concrete auditor defect: a one-to-one identity mapping incorrectly passed as
anonymization. The new regression first failed because the CLI returned 0. The
auditor now rejects each unchanged target with `target_not_anonymized`, retaining
the existing nonempty-string and one-to-one checks. The regression verifies exit
1, zero accepted bindings, no disclosed source targets, and unchanged corpus and
fixture bytes. The same four-file Node command above now passes **23/23**, without
failures or skips. Both changed JavaScript files pass `node --check`, and
`git diff --check` passes. No Python or workflow files changed. This correction
advances binding validation; it does not recover historical provenance or
establish operational acceptance.

The blocker comment and additive `needs-human` label were also attempted through
the connector and rejected by the same approval policy. No remote mutation
succeeded. Staging these three changed files failed with
`.git/index.lock: Read-only file system`, so this iteration could not create the
required source commit. The changes remain in the working tree for collection
by the automation's committing process.

## Read-only production observation collector

`scripts/observe_redirect_runtime.js` advances the third task by collecting the
existing free screen together with runtime artifact hashes, without running a
role, linking outcomes, taking writer locks, or changing cadence or caps. Run
from the source checkout being observed on the owner's host:

```sh
node scripts/observe_redirect_runtime.js /path/to/owner-runtime /path/to/stamp-state /opt/anaconda3/bin/python3
```

The first path is required; state defaults to that path and Python defaults to
`python3`. Existing corpus, stage-2 plan and report-directory environment overrides
are honored. The sweep is read from the selected state directory. The collector
uses `redirect_apply.py --screen --json`, whose candidate population and refusal
rules remain authoritative. It avoids `--status`, since its default outcome-link
preview can create or migrate the Brain. The source hash identifies the observer
checkout, not an independently authenticated deployed revision.

Blocking deficits and drainable/current candidate counts come from that screen.
Unavailable or invalid corpora stop observation; stale or missing populations
remain unknown. Inputs are hashed before and after execution, including cadence
stamp modification times; changes invalidate the counts without blocking the
writer. The collector hashes private target, role and refusal identities and
retains corpus line bindings. Logged numeric summaries retain their original
run timestamps separately from the new observation timestamp. Only the latest
twenty log runs are summarized, with the total retained run count printed.
Recorded applies/links are not qualified influence edges or stall resolution.
The invoking process's flag is explicitly distinguished from the deployed tick's
unknown flag; source defaults supply no production evidence.

The actual runner invocation is retained in
[`issue-1088-runtime-observation.json`](issue-1088-runtime-observation.json).
Corpus, plan, sweep, cadence success/failure markers, apply log and switch-review
artifact are missing here. Blocking count: **unknown**; drainable count:
**unknown**; current candidates: **unknown**, not candidates 0. No November 3
production break, attributed apply, resolution timing or control was established.
Private historical receipts remain unavailable. The selected interpreter still
lacks pytest; a fresh temporary installation attempt found no available pytest
distribution. The first four tasks remain unchecked and operational acceptance
remains incomplete. This capture is a new runner observation, not a historical
replay or an owner-host observation.

All **30** scoped Node checks pass without skips, including seven new collector
checks. Real screen executions against manufactured empty, refused and stale
populations confirmed the corresponding zero, blocked and unknown results and
unchanged runtime bytes. They establish no production population. This runner
again returned `EPERM` alongside zero child exits; the collector rejects those
executions rather than promoting their output to verified counts. Tests also
cover sanitized lineage/refusal bindings, changing inputs, malformed corpus and
screen output, process failures, and missing owner state. Both new JavaScript
files pass syntax checks. No Python or workflow files changed.

The source commit was attempted, but staging again failed with
`.git/index.lock: Read-only file system`. The connector rejected both the blocker
comment and additive `needs-human` label with `MCP tool call requires approval,
but approval policy is never`. No commit or remote mutation succeeded; these
changes remain for the automation's committing process.

## Source-target permutation regression

On November 3, reviewed commits `1df14c5` through `89608f6` and reran the
30 existing scoped Node checks successfully. Reconciliation remains the local
checklist above: private recovery and the first four distinct tasks are still
open. The PR-body update, blocker comment and additive `needs-human` label were
each rejected with `MCP tool call requires approval, but approval policy is never`.
No remote checklist change succeeded. Both named private receipts and the runner's
expected corpus path remain absent, and the repository-selected interpreter still
reports `No module named pytest`.

A manufactured control exposed another binding-validation failure: rotating
the original source targets across fixtures retained a one-to-one mapping and
changed every individual target, yet the auditor exited 0. The regression failed
on that exit status before the correction. The auditor now rejects any fixture
target present in the stalled-source target set, including targets belonging to
additional unbound source reports. It reports `target_not_anonymized` without
printing those targets. The permutation control also verifies corpus and fixture
bytes remain unchanged. These controls establish no historical provenance.

The five-file Node command (the four-file command above plus
`tests/test_observe_redirect_runtime.js`) now passes **32/32**, without skips.
JavaScript syntax checks and `git diff --check` pass. No Python or workflow files
changed. Historical provenance, an October 20 receipt, and operational acceptance
remain unestablished; blocking, drainable and candidate counts remain **unknown**.
This correction does not establish a production failure or a live action.

Staging the validated source, tests and report failed with
`.git/index.lock: Read-only file system`; no commit was created by this iteration.
