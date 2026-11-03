# Issue #1106 execution evidence

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
