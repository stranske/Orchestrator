# Issue #1106 execution evidence

At `2026-11-03T23:37:48.131Z`, the runner still cannot access the owner path;
blocking, drainable and candidates remain unknown. Historical inspection and
real-stall acceptance remain unmet. A bounded connected-storage search for
`redirect-apply-link` returned October test artifacts, not November 3 production
evidence; the retrieved issue #1106 comments contain no owner-host capture.

A new real-Python screening regression checks an eligible control, another
agent's held claim, a held claim combined with the corpus daily cap, the cap
alone, and restored capacity. It verifies exact hashed refusals, current capture
dates, unchanged runtime bytes and retention of the held claim directory.
The initial failure exposed an unsupported test override: `claims.py` reads
`HANDOFF_DIR/claims`, not `ORCH_CLAIMS_DIR`. The sandbox now uses `HANDOFF_DIR`.
All **52** scoped Node tests passed without failures or skips on Node v24.22.1
with real Python 3.14.9 screening. See [failure output](observation-bounds-red.txt),
[passing output](observation-bounds-green.txt) and the
[dated validation receipt](observation-bounds-validation.json). These are
manufactured controls, not a production apply or an operational control comparison.
No new offline exact-revision execution is claimed, and no Python files changed.

Remote reconciliation is still blocked: the connector rejected the PR body,
comment and additive `needs-human` label because approval is required while the
run's approval policy is `never`. PR #1107 remains open and ready (`draft=false`),
with its active `Closes #1106` directive unchanged. The local proposal retains
the verified checkboxes and a nonclosing reference; operational tasks stay open.
The required commit attempt failed at staging with `.git/index.lock: Read-only
file system`. This round's test and evidence changes remain uncommitted for
automation to collect.

Completion-tracking validation at `2026-11-03T23:30Z` adds three regression
checks against the retained receipts and both checklists in the proposed PR
body. They require clean named/focused/replay proof for offline completion,
preserve unknown owner counts and separate observation dates, and leave
historical inspection, real-stall acceptance and remote reconciliation open.
All **51** scoped Node tests passed without failures or skips; see
[actual output](completion-tracking-node-tests.txt) and the
[validation receipt](completion-tracking-validation.json). This validates
existing evidence; no new owner-host observation or offline execution is claimed.
The PR body correction, blocker comment and additive `needs-human` label were
again rejected because approval is required while the run's approval policy is
`never`. PR #1107 was re-read open and ready (`draft=false`), with the active
`Closes #1106` directive still present. Remote reconciliation remains unchecked.
After verification, the required commit attempt failed while staging with
`.git/index.lock: Read-only file system`; this round's test and evidence changes
remain uncommitted for automation to collect. No Python files changed.

Observed November 3, 2026, in the hosted runner. Operational acceptance remains
incomplete. No owner-host deployment, real apply, or resolved stall is established.

The first task now has successful offline execution against merged PR #1089,
revision `e86313cb6f1f6f047896b5c1bbb5d9c3c68d339e`, reachable from the recorded
`origin/main` revision `e58ba8e23abbe5d7888f3bca94796fe07ffdb427`.

| Execution | Exit | Actual result |
| --- | --- | --- |
| Interpreter identity | 0 | Python 3.14.8 |
| Required named nodes | 0 | 2 passed |
| Both focused files | 0 | 25 passed |
| Offline replay | 0 | authorized 12/12 with named agents (dry-run) |

Every execution in [merged-revision-after.json](merged-revision-after.json) has
null signal and error code. The observation started at
`2026-11-03T22:49:12.041Z`. Both pytest commands use `-m "not slow"`.
The archive SHA256 is
`7e4e0aa7f258787e9e31dcdb4840563453d24a2685c9161dc45b9ba67684333f`.
The archive contains the merged source and test trees plus `pyproject.toml`;
dirty working-tree Python was not executed. Temporary operational state, disabled
apply/heartbeat flags, and archive cleanup remain enforced.

The repository-selected interpreter initially lacked pytest, and the owner
Anaconda interpreter is absent. Online installation found no available
distribution. Existing cached pytest and dependency wheels were installed
offline into `/tmp/issue-1106-python`, a virtual environment of the selected
`/opt/hostedtoolcache/Python/3.14.8/x64/bin/python3`.
[interpreter-preflight.json](interpreter-preflight.json) retains interpreter
identity, pytest 9.1.1, wheel hashes, and the repaired verifier's hash.

## Concrete execution failure and repair

The unmodified merged verifier executed the same real tests and replay, but
reported `EPERM` alongside zero exits for all four Python commands. Its aggregate
correctly remained blocked; [merged-revision-before.json](merged-revision-before.json)
retains that actual output. Independent synchronous and asynchronous child probes
reproduced the difference in this runner.

The first source repair replaces synchronous launches in this repository-specific
verifier with asynchronous `execFile` calls. Arguments remain separate from the
executable; timeout and output bounds, binary archive transport, process-error
rejection, test identity checks, ancestry checks, and cleanup remain in place.
The after receipt uses this unmerged launcher repair against the unchanged
merged Python archive. It does not claim that the original merged verifier
completed cleanly. Previous issue-1088 receipts remain unchanged.

Three new tests exercise actual child success/failure, unavailable executables,
timeouts, output overflow, and binary stdin. The existing manufactured controls
still reject process errors even with zero exits. All 54 scoped Node tests pass
without failures or skips; [node-tests.txt](node-tests.txt) retains their output.

## Owner-host evidence remains unavailable

[storage-search.json](storage-search.json) records connected-storage searches for
the corpus, apply log, sweep, November 3 switch review, and armed cadence. Matching
paths are October pytest controls, not authentic November 3 production artifacts.
No authentic owner-host evidence was recovered. These search results do not prove
that the owner's artifacts never existed or exhaust every possible source.

[owner-path-observation.json](owner-path-observation.json) records a read-only
collector invocation against `/Users/teacher/.codex/orchestrator` **from this
runner**, where that owner path is unavailable. Blocking, drainable, and eligible
candidate counts remain **unknown**, never candidates 0. Deployed flag, cadence,
refusals, lineage, acted_on count, resolution time, and control comparison remain
unestablished. Historical November 3 owner-host acceptance remains unmet. Any later
observation must retain its own date and cannot qualify as historical evidence.

[collector-compatibility.json](collector-compatibility.json) verifies the source
fingerprinting API on this runner's actual Node v24.22.0: 237 Python files were
read successfully. Collector tests also exercise actual screening. The owner's
Node runtime remains unknown; compatibility there cannot be inferred from this.
No live flag, cadence, writer lock, or daily cap was changed.

## Completion reconciliation

PR #1089 is merged at the revision above. Its checked November 3 inspection task
and closure directive do not establish operational acceptance. Issue #1106 remains
the open operational follow-up; this work must not close it automatically.

- [x] Execute the required exact-revision tests and replay with clean process results.
- [ ] Inspect authentic November 3 owner-host flag, cadence, population, refusals and lineage.
- [x] Retain unavailable historical evidence explicitly, without backdating observations.
- [ ] Observe an eligible real stall through bounded apply, review, resolution and control.
- [x] Repair only the printed execution failure and check the available Node runtime.
- [ ] Reconcile remote tracking and closure directives; the connector rejected the update.

The offline acceptance criterion is verified. The unavailability/date-separation
criteria are documented, while the historical production criterion and real-stall
criterion remain unmet. Completing them requires authentic owner-host artifacts
and observation through the existing bounded cadence.

The PR #1107 body update, blocker comment, and additive `needs-human` label were
each rejected with `MCP tool call requires approval, but approval policy is never`.
No remote update succeeded. The proposed body would check the verified offline,
limitation, repair, and date-separation items, retain unchecked operational tasks,
and replace `Closes #1106` with a nonclosing reference. Remote tracking therefore
remains unfinished. PR #1089 is already merged and was not reopened or changed.

In the initial turn, staging the source, tests, and receipts failed with
`.git/index.lock: Read-only file system`. No commit or push was created; the
meaningful source change remains in the working tree for the automation to collect.

## Collector follow-up on November 3

The owner-host inspection remains blocked. The retained storage search recovered
no authentic snapshot, and the later runner invocation at
`2026-11-03T23:04:03.253Z` still found the owner path unavailable. See
[owner-path-observation-followup.json](owner-path-observation-followup.json).
Blocking, drainable and candidates remain unknown. This is a later runner check,
not a recovered historical owner-host observation or proof that artifacts never
existed. Historical production acceptance and real-stall acceptance remain unmet.

A manufactured runner control printed a concrete collector failure: screen exit
zero with error `EPERM`. [collector-process-before.json](collector-process-before.json)
retains it; [collector-process-red.txt](collector-process-red.txt) records the
strengthened real-screen regression test failing before the repair. This
repository-specific evidence collector now shares the verifier's bounded
asynchronous launcher and awaits screening before checking artifact stability.
Process errors still invalidate counts, and no workflow infrastructure was edited.

[collector-process-after.json](collector-process-after.json) records a clean exit,
no signal or process error, an unchanged 237-file Python source fingerprint and
unchanged manufactured runtime bytes on Node v24.22.1. Its candidates 0 belongs
only to the explicitly manufactured empty population. The real-screen regression
also verifies refused and stale populations and an unavailable interpreter.
[collector-cli-control.json](collector-cli-control.json) verifies the actual CLI
awaits a successful screen and exits zero. The owner's Node version remains unknown.

All **46** scoped Node tests passed without skips using:

```text
node --test --test-isolation=none tests/test_redirect_stall_provenance.js tests/test_verify_redirect_merged_revision.js tests/test_observe_redirect_runtime.js tests/test_redirect_recovered_receipt.js
```

[collector-node-tests.txt](collector-node-tests.txt) retains the output. No Python
files changed. No production flag, cadence, writer lock or daily cap changed, and
no real apply, acted-on review, resolution timing or control comparison is claimed.

The connector again rejected the PR body update, blocker comment and additive
`needs-human` label because approval is required while approval policy is `never`.
[proposed-pr-body.md](proposed-pr-body.md) retains the concrete proposed correction:
check only verified offline/limitation/repair/date items, leave operational tasks
and remote reconciliation unchecked, and replace the automatic closure directive
with a nonclosing reference. Remote tracking remains unfinished. PR #1107 was
verified open with `draft=false` before the rejected updates.

Staging this follow-up also failed with `.git/index.lock: Read-only file system`.
The collector source, tests and new receipts remain uncommitted for the automation
to collect; this turn could not satisfy the requested commit requirement.

## Date-separation regression verification on November 3

The retained owner-host inspection is still blocked: the existing search found
only October test fixtures, and both runner observations left blocking, drainable
and candidate counts unknown. No new owner-host observation is claimed here.

On November 3, 2026, the scoped Node suite passed **47 tests**, with no
failures or skips, on Node v24.22.2. Actual output is retained in
[date-separation-node-tests.txt](date-separation-node-tests.txt). The command is
the same four-file scoped command recorded above.

The new regression uses explicitly manufactured November 3 records and simulated
November 5 and 6 clocks. Those simulated dates are test inputs, not observation
receipts. It verifies that each capture receives its own date while corpus mtime,
lineage time, log time and review time stay separate. Even positive archived
apply and acted-on review records cannot establish historical deployment or
real-stall acceptance. A stale population remains unknown; removing the corpus
also leaves blocking unknown and prevents screening. The invoking process flag
is not replaced by the archived flag. All captures preserve the runtime bytes.

This test advances the verified limitation/date-separation task already checked
above. Authentic historical inspection, a real bounded apply-to-resolution chain,
and remote completion reconciliation remain unchecked. No production flag,
cadence, writer lock or daily cap changed, and no Python files changed.

The required commit was attempted after verification. Staging failed with
`.git/index.lock: Read-only file system`, so this round's test and evidence
changes remain uncommitted for the automation to collect.

## CLI receipt follow-up on November 3

A real subprocess regression exposed another concrete collector failure: exit 1
with empty stdout, causing JSON parsing to fail. The restricted runner reproduced
lost asynchronous Node pipe output with both console and stream writes, while a
synchronous write reached the parent. The collector now finishes writing its JSON
receipt synchronously before returning the CLI exit status. The bounded child
launcher and free screen are unchanged. This repair is local, not a merged revision.

[collector-cli-partial-state-red.txt](collector-cli-partial-state-red.txt) retains
the actual failure. [collector-cli-partial-state-green.txt](collector-cli-partial-state-green.txt)
retains all **48** scoped tests passing without skips using the four-file Node
command above, on Node v24.22.1. The added real subprocess test uses manufactured
missing, invalid, stale and verified empty populations. It checks complete JSON,
nonzero unavailable exits, current observation times, private-value exclusion,
retained positive review/log records without operational credit, and unchanged
runtime bytes. Only the verified empty control reports candidates 0.

[collector-cli-followup.json](collector-cli-followup.json) records the real runner
check at `2026-11-03T23:23:35.294Z`: the receipt is now emitted, exit is 1 without a
signal or process error, and all 237 Python source files are fingerprinted. The
owner path is still unavailable; blocking, drainable and candidates remain unknown.
This later runner check is not an owner-host observation or recovered historical
artifact. No deployed flag, cadence, writer lock or daily cap was changed.

[collector-cli-recovery-search.json](collector-cli-recovery-search.json) records
the follow-up storage search and issue-comment inspection. Returned files predate
the requested November 3 lower bound; search-filter enforcement and completeness
cannot be assumed. The exact apply-log matches are October pytest controls. No
authentic November 3 owner-host artifact or eligible real stall was recovered.
The historical inspection and real-stall tasks remain unchecked.

The reviewed commits verify the existing offline, limitation, repair and date
items in the proposed PR body. Reconciliation remains unchecked: the connector
again rejected the body correction, blocker comment and additive needs-human label
because approval is required while the run's approval policy is never. The local
proposal retains a nonclosing issue reference; the remote closure directive is
still a blocker. No Python files changed in this follow-up.

After verification, the required staging/commit attempt failed with
`.git/index.lock: Read-only file system`. This round's source, test and evidence
changes remain uncommitted for automation to collect. Final connector verification
confirmed PR #1107 remains open with draft=false at `f02fbe7`; the remote
`Closes #1106` directive remains present because its correction was rejected.
