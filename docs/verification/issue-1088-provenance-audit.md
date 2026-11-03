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
The observer also records a byte fingerprint of every Python source file under
the local `src/` tree, including nested packages, before and after screening.
Changed source bytes or file membership invalidate the capture; an unreadable
tree prevents screening. This does not authenticate external dependencies or the
deployed revision, and a change restored before the final read can escape detection.

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

## Recorded switch-review chain capture

Reviewed the five implementation commits through `a57f1b6` and verified all 32
existing scoped Node checks. The reconciliation above still applies: the first
four distinct tasks remain open; retaining the historical-provenance limitation
and separating observations from historical execution remain complete. The
connector again rejected the PR-body update, blocker comment and additive
`needs-human` label with `MCP tool call requires approval, but approval policy is
never`. No remote checklist or label change succeeded.

The existing runtime collector now extracts the single `redirect-apply-bootstrap`
row from the retained switch review's `value_chain.rows`. It retains the report
and monitor timestamps, rolling window, numeric chain counts (including
`acted_on`), and the monitor's recorded `first_break`. It does not recompute a
diagnosis or equate demand with current eligibility. Missing counts remain null;
missing, disabled, malformed and duplicate rows cannot supply a zero. Only the
known numeric fields and diagnosis vocabulary are emitted. A review rewritten
during screening invalidates the retained counts. The existing file hash binds
these values to the captured artifact; a recorded positive `acted_on` alone does
not establish an attributable apply, stall resolution, or operational acceptance.

Three new tests use manufactured reports, including demand 18 with current
candidates 0, positive recorded activity with an unavailable current corpus,
unknown counts, invalid rows, and a concurrent rewrite. The five-file Node
command above now passes **35/35**, with no skips. Both changed JavaScript files
pass syntax checks and `git diff --check` passes. No Python or workflow files
changed.

A new exact-revision observation at `2026-11-03T16:59:59.339Z` remains blocked on
merged revision `8f596dc01e1ede350724c1b88bc36610380af79b`: both pytest invocations
exit 1 with `No module named pytest`. Offline replay exits 0 with
`authorized 12/12 with named agents (dry-run)`, while the subprocess layer still
reports `EPERM`. Installing pytest again failed. At
`2026-11-03T17:00:00.498Z`, the owner corpus and switch-review artifact were missing:
blocking, drainable and candidates remain **unknown**, not candidates 0. The
private receipts also remain absent. These are new runner observations, not an
October 20 receipt or production evidence. No bounded live action was attempted.

Staging these source, test and report changes failed with `.git/index.lock:
Read-only file system`; this iteration could not create the required commit.
The validated changes remain in the working tree for the automation's committing
process. PR #1089 was re-read and remains open, `draft=false`, at `a57f1b6`.

## Report-directory capture integrity

The first task remains blocked on the two private receipts and original corpus;
neither the public #582 discussion nor the #595 discussion supplies their contents.
At `2026-11-03T17:10:38.394Z`, the audit again returned `unavailable` with
`private_source_corpus_unavailable`. The historical-provenance limitation remains
explicitly retained, but recovery and an October 20 execution remain unverified.

An observed collector defect allowed a report added during screening to retain
`status: observed`. Its regression failed on `observed` versus `unavailable`
before the correction. The observer now fingerprints sorted report-directory
entry names, sizes and modification times before and after screening, using the
same directory passed to `--report-dir`. It emits only the fingerprint, preserving
private filenames. A changed fingerprint or directory availability returns
`artifacts_changed_during_capture` and `changed_artifacts: ["report_dir"]`, with
blocking, drainable and candidate counts unknown. This is a metadata integrity
check, not a filesystem snapshot; changes that restore identical metadata can
escape it. No production-chain defect or live action is established by this fix.

All **37** scoped Node checks pass with zero failures or skips. The two new tests
cover default and overridden directories, report creation/removal, size and mtime
changes, directory disappearance/creation, deterministic ordering, unavailable
paths, private output and read-only execution. These inputs are manufactured
controls, not historical evidence. Both changed JavaScript files pass syntax
checks; `git diff --check` passes. No Python or workflow files changed.

At `2026-11-03T17:10:30.174Z`, exact merged revision
`8f596dc01e1ede350724c1b88bc36610380af79b` still could not execute either pytest
invocation: the selected Python 3.14 interpreter lacks pytest, and installation
failed. Offline replay printed `authorized 12/12 with named agents (dry-run)`;
the subprocess layer also reported `EPERM`. The aggregate remains blocked.
At `2026-11-03T17:10:31.338Z`, owner runtime artifacts, including the report
directory, were missing. Blocking, drainable and candidate counts remain
**unknown**, not candidates 0. These new runner observations do not satisfy an
October 20 receipt or the November 3 production-chain acceptance.

Staging the source and tests failed with `.git/index.lock: Read-only file system`;
no commit could be created. The connector also rejected the checklist correction
and additive `needs-human` label with `MCP tool call requires approval, but
approval policy is never`. The local reconciliation above remains authoritative
for this session: the production-inspection task is incomplete, and the retained
historical-provenance limitation is verified. Operational acceptance remains open.

## Source-change capture regression

The open review finding about source changes was reproduced: a simulated edit
to `redirect_apply.py` during screening incorrectly retained `status: observed`.
The observer now rejects changes to that entry point or its local imported
modules with `changed_artifacts: ["screen_sources"]`, leaving blocking, drainable
and candidate counts unknown. Three new tests cover the failing observation,
nested source bytes and membership (including equal-size edits with restored
mtimes), unavailable sources, read-only behavior and sanitized output. All **40**
scoped Node checks pass without failures or skips; syntax checks and
`git diff --check` pass. No Python or workflow files changed.

The original private receipts and owner runtime are still unavailable. The
selected interpreter still lacks pytest, and installing it into `/tmp` failed
with no available distribution. No historical evidence, eligible real stall,
attributed apply, control comparison or production-chain defect was established.
The first four distinct tasks remain incomplete; no new checkbox was completed.

At `2026-11-03T17:21:19.885Z`, the actual runtime observation returned
`owner_corpus_unavailable`; blocking, drainable and candidates remain **unknown**.
Staging the validated source, tests and report failed because `.git/index.lock`
is on a read-only filesystem, so no commit was created. The connector rejected
the production-task checkbox correction, blocker comment and additive
`needs-human` label with `MCP tool call requires approval, but approval policy is
never`. No remote update succeeded.

## Persisted refusal and outcome-link dispositions

At `2026-11-03T17:38:43Z`, the first task remains blocked: the private corpus
and original receipts are unavailable, and the fetched #582/#595 discussions
do not expose their contents or a genuine October 20 receipt. The runtime
observation again returns `owner_corpus_unavailable`; blocking, drainable and
eligible-candidate counts remain **unknown**, not candidates 0.

The observer dropped persisted apply-refusal details and outcome-link acceptance,
and represented absent boolean facts as false. Two new regressions failed before
the correction. It now retains accepted/rejected and linked/synced dispositions,
with absent or malformed boolean facts represented as null. Authorization blocks,
apply refusals and outcome-link failure reasons are retained as hashes alongside
the existing source-line, entry, role and downstream-run bindings. Persisted
refusals remain separate from current-screen refusals; a stored link is not proof
of acceptance or resolution. All **42** scoped Node checks pass without failures
or skips, including privacy and read-only assertions. Both changed JavaScript
files pass syntax checks, and `git diff --check` passes. No Python or workflow
files changed. These manufactured controls establish observer behavior only.

The exact-revision verifier was also rerun on merged
`8f596dc01e1ede350724c1b88bc36610380af79b` using the selected Python 3.14
interpreter. Both pytest invocations still exit 1 before collection because
pytest is unavailable; a temporary installation attempt could not obtain it.
Replay exits 0 and prints `authorized 12/12 with named agents (dry-run)`, with
`EPERM` also reported by the subprocess layer. The aggregate remains blocked.
No production-chain defect, attributable live apply, resolution timing or
control comparison was established. No new task checkbox is completed, and
operational acceptance remains incomplete. These are new runner observations,
not an October 20 receipt or a capture of the owner's November 3 production chain.

Staging these three validated files failed with `.git/index.lock: Read-only file
system`, so no commit was created. The connector rejected the production-task
checkbox correction, blocker comment and additive `needs-human` label with
`MCP tool call requires approval, but approval policy is never`. No remote update
succeeded. PR #1089 remains open and ready for review at `a28d88e`.

## Byte-preserving provenance validation

The first recovery task remains blocked. Re-reading the #582 and #595 discussions
recovered only the already documented private receipt paths and owner assertions;
the private files and any genuine October 20 execution receipt remain unavailable.
No new task checkbox is complete.

A concrete auditor defect was reproduced with manufactured controls: invalid
UTF-8 in private targets or a fixture target was silently replaced during decoding,
and the CLI returned success. Source-entry hashes could consequently describe
replacement bytes instead of the supplied source-line bytes. The corpus regression
failed first; after the corpus correction, the fixture regression failed separately.
The auditor now validates UTF-8 before decoding either input. Invalid corpora
return `unavailable` with `invalid_source_corpus_utf8`; invalid fixtures cannot
receive bindings. The original corpus byte hash remains available for diagnosis.
No source contents, private targets, or replacement strings are printed.

All **44** scoped Node checks pass without failures or skips, including the new
invalid-byte and valid multibyte controls, privacy and immutability assertions.
Both changed JavaScript files pass syntax checks; `git diff --check` passes.
No Python or workflow files changed. This corrects audit integrity and establishes
neither historical bindings nor a production-chain failure.

At `2026-11-03T18:36:50.812Z`, merged revision
`8f596dc01e1ede350724c1b88bc36610380af79b` was re-evaluated with the selected
Python 3.14 interpreter. Both named tests and both focused files remain blocked
before collection by missing pytest; installation into `/tmp` failed with no
available distribution. Replay printed `authorized 12/12 with named agents
(dry-run)` and exited zero, while the subprocess layer also retained `EPERM`.
The aggregate remains blocked. At `2026-11-03T18:36:58.431Z`, the owner runtime
observation returned `owner_corpus_unavailable`: blocking, drainable and eligible
candidate counts remain **unknown**, not candidates 0. No live apply, resolution
timing or control comparison was established. These are current runner observations,
not an October 20 receipt or the owner's November 3 production-chain capture.

Staging the validated source, tests and report failed because `.git/index.lock`
is on a read-only filesystem; no commit was created. The GitHub connector rejected
the unverified production-task checkbox correction, blocker comment and additive
`needs-human` label with `MCP tool call requires approval, but approval policy is
never`. No remote mutation succeeded.
