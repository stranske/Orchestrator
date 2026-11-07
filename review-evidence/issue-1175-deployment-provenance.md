# Issue 1175: deployment provenance work

The exporter now marks output produced by the hosted challenger filter with
`exploration.challenger_filter = "hosted-runnable-alternatives/v1"`.

To retain deployment evidence, supply a JSON object containing `published_commit`
(full SHA), `deployed_at` (UTC timestamp with whole seconds), and
`deployment_evidence` (reference to the successful publication's audit record):

```sh
python3 src/route_weights_export.py --record-challenger-deployment /path/to/provenance.json --state-dir /path/to/state
```

The recorder fetches the export branch, requires complete history, and verifies
that the supplied commit is its first explicitly marked filter artifact. It
validates the challenger map and artifact timestamp, rejects missing or malformed
provenance, and preserves the first receipt. Deployment time must come from the
external audit record; artifact creation and Git commit times are not substitutes.
The recorder verifies Git history, not the external audit record's authenticity.

Local verification: 15 regression tests passed using standard-library unittest,
including real `publish_document` pushes and unchanged publications against
temporary bare remotes. The exporter's existing `--selftest` also passed.

No production acceptance checkbox is complete. This runner cannot reach GitHub,
has no production feedback database, and cannot establish the required fourteen
days of observation. The production query must run no earlier than 2026-11-08
and fourteen days after the verified deployment instant, whichever is later.

The required Anaconda interpreter, pytest, and Black are unavailable. Package
installation failed because DNS is unavailable. The mandatory Black formatting
gate therefore could not pass, so these Python changes must remain uncommitted
until Black and the required pytest checks can run successfully.

## CLI refusal verification (2026-11-05)

`tests/test_route_weights_export_cli.js` now exercises the real provenance command
in a child process for thirteen missing or malformed input cases. Each case runs
with both an absent and an existing receipt, checks exit status 1 and the expected
diagnostic, and verifies that no Git command, feedback database, or fallback state
is created and that any existing receipt stays byte-identical. All state is under
temporary directories. No hosted-pass instrumentation or reporting was restored.

Verification: `node tests/test_route_weights_export_cli.js` passed all 23 tests;
`PYTHONPATH=src python3 -m unittest discover -s tests -p
test_route_weights_export_deployment.py -q` passed all 15 tests. This round changes
JavaScript tests and this evidence note only; it does not change Python files.

The production observation remains blocked: this runner has no production feedback
database or verified production deployment receipt, and 2026-11-08 has not arrived.
Do not substitute fixture commits or timestamps for production provenance. The
Anaconda interpreter and pytest remain unavailable, so the named pytest acceptance
check is still unverified. No additional task checkbox is marked complete.

The main checkout's `.git` is read-only. This round's changes are committed in
`/tmp/orchestrator-1175-commit-8rMQYP`, with a handoff patch at
`/tmp/orchestrator-1175-cli-provenance.patch`; the main working tree retains the
same edits. Attempts to add `needs-human` and post the blocker on PR #1176 were
both rejected because those tools require approval and this run's approval policy
is `never`. A production operator must supply the verified publication audit
record and run the retained query after the observation period.

## Report edge-case verification and reconciliation (2026-11-05)

Reviewed commits `c0385ba`, `9c2ec36`, and `e977038`. They added the explicit
deployment recorder, isolated Git publication tests, shard registration, and CLI
refusal tests. They did not retain a verified production deployment receipt or
restore hosted-pass instrumentation. The reported 34-file change count does not
match this branch: its diff from `34c5354` before this round contained six files.

`hosted_exploration_pass_number` and `record_hosted_exploration_deploy` are absent
from the exporter, and hosted-pass reporting is absent from `switch_review.py`.
The two conditional tasks therefore require no instrumentation restoration.
Suggested reconciliation, applied consistently to both copies of the PR tasks:

- [x] Conditional hosted-pass helper retention task: helpers remain absent.
- [x] Conditional hosted-pass report restoration task: reporting remains absent.

The existing checked publication task verifies the recorder implementation only;
it does **not** establish a verified production deployment. Production provenance
and elapsed observation still require an operator. No production success is
claimed by these checkboxes. The named pytest acceptance checks remain unverified.

Added `tests/test_switch_review_exploration.js`, using the real Python
`format_report` through the existing JavaScript child-process testing approach.
Five passing cases cover `window=None` with no arm horizon, `window=None` with
suspect arms hidden, a measured window with each reason for hiding arms, and
visible arms without a window. The tests also verify that a suspect gate remains
visible, measured counts survive rendering, and no temporary state is created.
Only unrelated producer summaries are stubbed; the exploration renderer is real.
No hosted-pass helper is restored, so a helper-exception test is not applicable.

Verification:

- `node tests/test_switch_review_exploration.js`: 5 passed.
- `node tests/test_route_weights_export_cli.js`: 23 passed.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p
  test_route_weights_export_deployment.py -q`: 15 passed.
- `node --check tests/test_switch_review_exploration.js` and `git diff --check`: passed.

No Python files changed this round. Black and pytest are still unavailable, and
installing them failed because `pypi.org` could not resolve. The required Anaconda
interpreter and production feedback database are also absent. Do not run or claim
the production observation before both 2026-11-08 and fourteen days after a
verified deployment instant.

The PR-body reconciliation, blocker comment, and `needs-human` label were each
rejected with `MCP tool call requires approval, but approval policy is never`.
The remote PR body has therefore not been updated. PR #1176 was confirmed open
and ready for review before this round's attempted metadata mutations.

The checkout's `.git` remains read-only. This round's JavaScript test and evidence
are committed in `/tmp/orchestrator-1175-report-edges`, with a handoff patch at
`/tmp/orchestrator-1175-report-edges.patch`; the main working tree retains the edits.

## Recorder CLI recovery verification (2026-11-05)

Added an end-to-end case to `tests/test_route_weights_export_cli.js` that uses
the real provisioning, Git fetch/push, `publish_document`, and recorder CLI with
a temporary bare remote. It rejects an unmarked publication, publishes the real
filter contract, and forces receipt creation to fail by placing a directory at
the atomic-write path. The CLI returns failure without a receipt or traceback,
while the successfully published commit remains on the remote. After removing
the obstruction, an unchanged `publish_document` call and recorder CLI retry
recover the original audit timestamp and commit. Repeating the CLI preserves
both receipt bytes and modification time without republishing. No I/O or
publication functions are mocked; the feedback database remains absent and the
isolated home directory stays empty.

This is fixture verification, not production deployment evidence. No additional
task checkbox is complete. The production database is absent, and the observation
must still wait until both 2026-11-08 and fourteen days after verified deployment.
The required Anaconda interpreter is absent; the available Python also lacks
pytest, so the named pytest acceptance check remains unverified. No Python files
were changed in this round.

Verification: the CLI suite passed all 24 tests; the deployment unittest suite
passed all 15 tests; `node --check tests/test_route_weights_export_cli.js` and
`git diff --check` passed. PR #1176 was confirmed open and ready for review.

Attempts to add `needs-human` and post the blocker comment were rejected with
`MCP tool call requires approval, but approval policy is never`. Remote metadata
therefore remains unchanged. The checkout's read-only `.git` also prevents a
local commit; the tested edits are retained in the working tree and committed in
`/tmp/orchestrator-1175-cli-recovery`, with a handoff patch at
`/tmp/orchestrator-1175-cli-recovery.patch`.

## Remote verification failure and reconciliation (2026-11-05)

Reviewed all six branch commits through `98bb251`: seven files differ from the
base, rather than the reported 34. The first publication task is checked in the
PR's automated summary, but no verified production publication receipt has been
retained. Implementing a recorder does not complete that task; it should be
unchecked in both task lists. The two conditional instrumentation/reporting
tasks can stay checked, including in the duplicate list, because those extensions
remain absent. No production observation or named pytest acceptance task is done.

Extended the real CLI publication recovery test to make the bare remote
unavailable while a valid marked commit remains cached in `origin` refs. Before
any receipt exists, the CLI must refuse without creating one. After a receipt
exists, the same failure must preserve its bytes and modification time. Restoring
the remote allows verification to recover the original receipt without a new
publication. All state remains in temporary directories; no Python files changed.

The CLI suite passes all 24 tests; the deployment unittest suite passes all 15;
the report suite passes all 5. The new regression also rejects a disposable copy
of the recorder with its mandatory fetch removed. JavaScript syntax and diff
whitespace checks pass.

The production database, verified production audit record, and required Anaconda
interpreter remain absent. Pytest and Black are unavailable, and installing them
fails because `pypi.org` cannot resolve. The observation must wait until both
2026-11-08 and fourteen days after verified deployment. No substitute fixture
count or deployment timestamp is claimed.

PR-body reconciliation, the blocker comment, and the `needs-human` label were
rejected with `MCP tool call requires approval, but approval policy is never`.
Remote task checkboxes therefore remain unchanged.

The main checkout rejects Git index writes (`Read-only file system`). The tested
JavaScript and evidence changes are committed in `/tmp/orchestrator-1175-remote-verification-gicb0w62`,
with a handoff patch at `/tmp/orchestrator-1175-remote-verification.patch`. The main working tree retains the same edits.

## Multiple-publication CLI verification (2026-11-05)

Reviewed all seven branch commits through `8435684`; the branch changes seven
files, not the reported 34. The publication checkbox remains premature: the
recorder is implemented, but no verified production deployment receipt exists.
Both copies of that checkbox should remain unchecked. Both conditional helper
and reporting tasks are satisfied because those extensions remain absent.

Extended the isolated CLI regression with a second real `publish_document`
publication. The original audit still verifies without changing receipt bytes
or modification time. Claiming the later commit as the first deployment fails
with and without an existing receipt. Retrying the original audit recreates the
original receipt without publishing or rewinding the remote branch. This checks
that later exports cannot silently restart the production observation boundary.

Verification: all 24 CLI tests and all 15 deployment unittest tests pass;
JavaScript syntax and diff whitespace checks pass. A disposable exporter copy
that scans publication history newest-first fails the extended regression.
No Python source changed. The required Anaconda interpreter, pytest, Black,
production database, and verified production audit record remain unavailable.
No production or named pytest acceptance task is complete. Observation remains
gated on both 2026-11-08 and fourteen days after verified deployment.

The PR-body reconciliation, blocker comment, and `needs-human` label were each
blocked with `MCP tool call requires approval, but approval policy is never`.
The checkout also rejects Git index writes as read-only. The changes are retained
in the working tree; the isolated commit and handoff patch are at
`/tmp/orchestrator-1175-first-publication` and
`/tmp/orchestrator-1175-first-publication.patch`, respectively.

## Shallow-history CLI recovery verification (2026-11-05)

Extended the real publication CLI regression with a depth-one clone of the export
branch after two marked publications. The recorder refuses both the original
audit and a misleading audit naming the visible tip, with and without a retained
receipt. Refusal preserves existing receipt bytes and modification time. Fetching
the full history then rejects the later audit and recovers the original receipt
without changing the remote branch. All repositories, state, and home directories
remain temporary; no Python files changed.

Verification: all 24 CLI tests and all 15 deployment unittest tests pass.
Removing the shallow-history guard in a disposable exporter copy makes the CLI
regression fail. JavaScript syntax and diff whitespace checks pass.

No new task checkbox is complete. The production database, verified deployment
receipt, required Anaconda interpreter, pytest, and Black remain unavailable.
Observation still requires both 2026-11-08 and fourteen days after verified
deployment. The publication checkbox remains premature; no production receipt
has been retained. PR #1176 was confirmed open and ready for review.

The PR checkbox correction, blocker comment, and `needs-human` label were rejected
with `MCP tool call requires approval, but approval policy is never`. The checkout
also rejects Git index writes as read-only. The tested changes are retained in
the working tree and committed in `/tmp/orchestrator-1175-shallow-history-EHIk6w`,
with a handoff patch at `/tmp/orchestrator-1175-shallow-history.patch`.

## Independent malformed-publication verification (2026-11-05)

Reviewed branch history through `baa5d11`: seven files differ from the base,
rather than the reported 34. The malformed-challenger unittest shares history
between cases, so the first malformed artifact can mask the remaining inputs.
Added eight independent CLI regressions, each with its own temporary bare remote,
checkout, home, state, and audit. Cases cover missing/array challengers, missing or
extra task keys, hosted-unrunnable/non-string challengers, and missing/malformed
generation timestamps. Each refuses the original publication and a later repaired
artifact, both without a receipt and with an existing receipt that must remain
byte-identical with the same modification time. No production state is used.

Verification: the CLI suite passes all 32 tests; the deployment unittest suite
passes all 15; the report suite passes all five. JavaScript syntax and diff checks
pass. Removing the exact challenger task-set guard in a disposable exporter copy
makes both the missing-task and extra-task regressions fail at their assertions.
No Python changes are included in this round.

Reconciliation remains blocked remotely: the publication task must be unchecked
in both copies until a verified production receipt exists. Both conditional
helper/reporting tasks can be checked because those extensions remain absent.
Named pytest acceptance checks cannot be verified here: the Anaconda interpreter,
pytest, and Black are absent. A proposed reconciled body, leaving those named
checks unchecked, is retained at `/tmp/orchestrator-1175-reconciled-pr-body.md`.

The PR-body update, blocker comment, and `needs-human` label were rejected with
`MCP tool call requires approval, but approval policy is never`. PR #1176 remains
open and ready for review. No new production task is complete: the production
database and verified deployment audit are absent, and observation requires both
2026-11-08 and fourteen days after the verified deployment instant.

The checkout's Git index is read-only. This round's JavaScript test changes and
evidence are committed in a writable clone under
`/tmp/orchestrator-1175-independent-contracts-*`; the handoff patch is
`/tmp/orchestrator-1175-independent-contracts.patch`. The main working tree retains
the same edits.

## Retained-receipt integrity verification (2026-11-05)

Added six real CLI subtests for changed audit timestamps/evidence, conflicting
receipt epochs, missing receipt evidence, null receipts, and truncated receipt
JSON. Each case refuses without changing receipt bytes or modification time,
creating an atomic-write file, or publishing. Restoring the original fixture
evidence recovers the original receipt without changing the remote branch.
All state stays in temporary directories; no Python files changed.

Verification: `node tests/test_route_weights_export_cli.js` passed all 38 tests;
the deployment unittest suite passed all 15. JavaScript syntax and diff whitespace
checks pass. The production database and verified production deployment receipt
remain unavailable. Observation still requires both 2026-11-08 and fourteen days
after verified deployment. No additional task checkbox is complete; the checked
publication task remains premature. The Anaconda interpreter, pytest, and Black
are unavailable, so this run cannot verify the named pytest acceptance commands.

PR #1176 was confirmed open and ready for review. Adding `needs-human` and posting
the blocker comment were rejected with `MCP tool call requires approval, but
approval policy is never`; remote metadata remains unchanged. The main checkout's
Git index is read-only. The tested changes are retained here and committed in
`/tmp/orchestrator-1175-receipt-integrity-0UjvnW`, with a handoff patch at
`/tmp/orchestrator-1175-receipt-integrity.patch`.

## Export-branch merge provenance verification (2026-11-05)

Added a real CLI regression that rejects a valid filter commit while local, after
publication to another branch, and after becoming the export merge's second
parent. It also rejects the unpublished merge, then records the published merge
with the explicit audit timestamp. All repositories and state are temporary;
recording does not change the remote branch or create a feedback database.

Verification: all 39 CLI tests, 15 deployment unittest tests, and five report
tests pass. Removing first-parent traversal in a disposable exporter copy fails
the new regression by incorrectly accepting the side-branch commit. JavaScript
syntax and diff whitespace checks pass. No Python files changed.

No new acceptance checkbox is complete. The production database, verified
deployment audit, Anaconda interpreter, pytest, and Black remain unavailable.
Production observation requires both 2026-11-08 and fourteen days after verified
deployment. The publication checkbox still lacks production evidence. PR #1176
was confirmed open and ready for review. The main Git index is read-only; the
commit is retained in `/tmp/orchestrator-1175-merge-provenance-*` and the handoff
patch is `/tmp/orchestrator-1175-merge-provenance.patch`.

The PR checkbox correction, blocker comment, and `needs-human` label were rejected
with `MCP tool call requires approval, but approval policy is never`. Remote
metadata remains unchanged.

## Deployment-time boundary verification (2026-11-05)

Extended the real publication CLI regression to reject deployment one second
before artifact generation, both before recording and with an existing receipt.
Refusal preserves receipt bytes and modification time. Separate temporary state
directories verify that deployment at the exact generation second is accepted
with either `Z` or `+00:00`, retaining the supplied timestamp and correct epoch.
The merge-publication test now also verifies a valid all-null challenger map,
which represents no runnable alternative rather than a malformed contract.

Verification: all 41 CLI tests and all 15 deployment unittest tests pass;
JavaScript syntax and diff whitespace checks pass. No Python files changed.
No new acceptance checkbox is complete. The production database and verified
production deployment audit remain absent, as do the required Anaconda
interpreter, pytest, and Black. Observation must wait until both 2026-11-08 and
fourteen days after verified deployment. The checked publication task remains
premature because there is no retained verified production receipt.

PR #1176 was confirmed open and ready for review. The PR checkbox correction,
blocker comment, and `needs-human` label were rejected with `MCP tool call
requires approval, but approval policy is never`. Git index writes in this
checkout fail with `Read-only file system`. The tested edits remain here; the
commit is retained in `/tmp/orchestrator-1175-timestamp-boundary-Q9GMYh` and its
handoff patch is `/tmp/orchestrator-1175-timestamp-boundary.patch`.

## Rejected publication push verification (2026-11-05)

Extended the real CLI recovery regression with a temporary bare remote's
pre-receive hook that rejects the first `publish_document` push. The resulting
valid local filter commit cannot establish deployment provenance or create a
receipt. After explicitly retrying the Git push, an unchanged publication and
the existing receipt-failure recovery retain that exact commit and supplied
audit timestamp. All Git repositories, state, and home directories are temporary.

Verification: all 41 CLI tests, 15 deployment unittest tests, and five report
tests pass. A disposable recorder that prefers the local export branch fails
the new unpublished-commit assertion. JavaScript syntax and diff checks pass.
No Python files changed. The required Anaconda interpreter, pytest, and Black
remain unavailable; the named pytest acceptance commands are unverified.

No new acceptance checkbox is complete. Production provenance and observation
remain blocked by the absent production database and verified audit record;
observation must also wait until both 2026-11-08 and fourteen days after verified
deployment. The checked publication task remains premature. Adding `needs-human`
and posting the blocker comment were rejected with `MCP tool call requires
approval, but approval policy is never`. PR #1176 was confirmed open and ready.

The main checkout rejects Git index writes as read-only. The tested changes
remain in the working tree; the commit is retained in `/tmp/orchestrator-1175-rejected-push-bqqxu4sl`
and its handoff patch is `/tmp/orchestrator-1175-rejected-push.patch`.

## Subprocess bounds and immutable contract verification (2026-11-05)

Added timeout=60 to all Git subprocess calls in the recorder's `record_challenger_filter_deployment`
function (nested `git` helper), matching the existing timeout in `_fetch_export`. Added
`subprocess.SubprocessError` catching in the CLI that includes captured stderr, handling
text/bytes safely. The recorder now skips history revisions with absent EXPORT_PATH blobs
or JSONDecodeError artifacts while propagating other Git failures and continuing to reject
malformed explicitly marked contracts.

Replaced validation against mutable router sets (`router.KEEPALIVE_AGENTS`, `router.RESERVE_AGENTS`,
`router.BACKUP_AGENTS`) with a documented immutable v1 challenger/task contract
(`CHALLENGER_V1_RUNNABLE_AGENTS = frozenset({'codex', 'claude', 'cursor'})`) in
`record_challenger_filter_deployment`, matching publication-time semantics and preserving
hosted-unrunnable rejection (gemini).

Added focused temporary-repository tests in `tests/test_route_weights_export_deployment.py`
for deletion/invalid-JSON history verification, and in `tests/test_route_weights_export_cli.js`
for timeout/error diagnostics and unchanged receipt recovery.

The `tests/test_switch_review_exploration.js` tests exercise the real Python renderer
through Node child processes, verifying that Python dependencies are available for the
renderer regression tests.

Verification:
- `/opt/anaconda3/bin/python3 -m pytest -q tests/test_route_weights_export_deployment.py 
  tests/test_route_weights_export_exploration_rate.py`: 23 passed, 26 subtests passed
- `PYTHON=/opt/anaconda3/bin/python3 node --test tests/test_route_weights_export_cli.js 
  tests/test_switch_review_exploration.js`: 48 passed
- `python3 src/route_weights_export.py --selftest`: OK

Recorder implementation is complete and verified. Production deployment/observation remains
incomplete: no verified production audit record exists, and observation requires both
2026-11-08 and fourteen days after verified deployment. The named pytest acceptance checks
are now verifiable with Python dependencies available.

## Closer follow-up: recorder scan, timeouts, CI Gate, threads (2026-11-06)

**Recorder vs production acceptance.** Completion of the deployment recorder (history scan,
CLI, subprocess bounds, tests) is separate from production acceptance. Production acceptance
stays explicitly incomplete until there is authentic deployment evidence, a verified deploy
epoch from the publication audit, and a mature production query run only after both
2026-11-08 and fourteen days after verified deployment—not expanded further while waiting
for that observation window.

Code review evidence (current branch head before commit/push):

- **Exporter history scan** (`record_challenger_filter_deployment`): Before `git show` on each
  candidate commit, `ls-tree -r --name-only <candidate> -- <EXPORT_PATH>` skips absent paths;
  invalid JSON still skips with `JSONDecodeError`; unrelated Git failures remain fatal
  (`ls-tree` or `show` non-zero with path present still propagates).
- **CLI timeout**: `subprocess.TimeoutExpired` is handled explicitly with stderr
  `deployment provenance refused: git subprocess timed out (...)` (no traceback). Node tests
  inject `TimeoutExpired` at fetch vs post-fetch Git boundaries and assert that diagnostic,
  no new receipt, and receipt byte/mtime preservation.
- **Gate / switch-review renderer**: New job `orchestrator node tests` in
  `.github/workflows/pr-00-gate.yml` installs `requirements.lock`, sets `PYTHONPATH=src`, then
  runs `node --test tests/test_route_weights_export_cli.js tests/test_switch_review_exploration.js`.
  `required-contexts.json` includes the check name `orchestrator node tests`.
  `gate_summary.py` records `ORCHESTRATOR_NODE_TESTS_RESULT` so a failed job fails the Gate verdict.
- **PR #1176 review threads (three active)**: Resolved on GitHub after verifying the above:
  `ls-tree` pre-check on `src/route_weights_export.py`; timeout injection and diagnostics on
  `tests/test_route_weights_export_cli.js`; Gate registration for
  `tests/test_switch_review_exploration.js`.

Verification (focused, as requested):

- `/opt/anaconda3/bin/python3 -m pytest -q tests/test_route_weights_export_deployment.py tests/test_route_weights_export_exploration_rate.py` → **22 passed, 26 subtests passed**.
- `PYTHON=/opt/anaconda3/bin/python3 node --test tests/test_route_weights_export_cli.js tests/test_switch_review_exploration.js` → **ℹ pass 48**, **fail 0** (duration ~62s).

Additional file-touch validation: `tests/test_delivery_required_contexts.py` (1 passed) for
`required-contexts.json` and `_gate_extra_pull_request_contexts()`.

## Publication-history read failure regression (2026-11-06)

Extended the real publication/recovery CLI test with eight cases covering failures
of `git ls-tree` and `git show`, text and invalid-UTF-8 byte diagnostics, and both
absent and retained deployment receipts. The fault occurs after the real fetch
and history listing. Each case requires a refusal with the original Git error,
preserves receipt bytes and modification time, and recovers from the original
audit without republishing or changing the deployment instant. All repositories,
home directories, databases, and state paths remain temporary. No Python source
or protected files changed.

Verification: `node tests/test_route_weights_export_cli.js` passed all 51 tests;
the deployment unittest suite passed all 16 tests; the switch-review Node suite,
JavaScript syntax check, and diff whitespace check also passed. Disposable copies
that swallow either tree or blob read errors fail the new regression.

No task checkbox is newly complete. This runner lacks the production database
and authentic deployment audit, and the observation requires both 2026-11-08 and
fourteen days after verified deployment. `/opt/anaconda3/bin/python3`, pytest,
and Black are absent; installing the missing packages failed DNS. The named
pytest acceptance check remains unverified in this run.

The checkout's `.git` is read-only and rejected `git add`. The tested JavaScript
change is retained as a commit in `/tmp/orchestrator-1175-history-read-commit`,
with a handoff patch at `/tmp/orchestrator-1175-history-read.patch`. Posting the
blocker comment and adding `needs-human` to PR #1196 were rejected with
`MCP tool call requires approval, but approval policy is never`.

## Truncated publication history regression (2026-11-06)

Reconciled commits `17a54f2` and `83c8e9c`: PR #1196 changes three files,
not the reported 34. No verified production receipt has been retained. The
first publication task and production observation criteria remain incomplete.
The two conditional implementation tasks are satisfied because the hosted-pass
helpers and reporting remain absent (verified in both source files):

- [x] Conditional hosted-pass helper retention task: helpers remain absent.
- [x] Conditional hosted-pass reporting task: reporting remains absent.

Updating both copies of these checkboxes in the PR body was rejected with
`MCP tool call requires approval, but approval policy is never`. The same
rejection prevented the blocker comment and `needs-human` label. Remote
checkboxes remain unchanged. PR #1196 was verified open with `draft=false`.

Added a real CLI regression with two marked publications and a depth-one
`file://` clone. Both the original and later commit are refused while history
is shallow, before and after a valid receipt exists. Restoring complete history
recovers the original publication and audit timestamp without changing receipt
bytes or modification time. With complete history, claiming the later commit
is also refused. The remote tip stays unchanged, the feedback database stays
absent, and all repositories, home directories, and state are temporary.

The publication CLI suite passed all 52 tests, the deployment unittest suite
passed all 16 tests, and the report CLI suite passed all 5 tests. The focused
shallow-history regression, JavaScript syntax, and diff whitespace checks also
passed. No Python or protected files changed.

Production observation remains blocked by the absent production database and
verified deployment audit, and must wait until both 2026-11-08 and fourteen days
after verified deployment. The Anaconda interpreter, pytest, and Black are absent;
the named pytest acceptance check remains unverified. This checkout's read-only
`.git` rejected staging. The changes are retained in an isolated commit at
`/tmp/orchestrator-1175-shallow-history-commit`, with a handoff patch at
`/tmp/orchestrator-1175-shallow-history.patch`.
## Artifact deletion and restoration regression (2026-11-06)

Extended the real publication/recovery CLI test with four cases covering a
deleted artifact and a byte-identical restoration, each with and without a
retained receipt. Neither later commit may replace the first publication.
Recording the original audit still recovers the original commit, deployment
instant, and receipt bytes, preserves existing receipt modification times, and
leaves the remote tip unchanged. All repositories, home, state, and database
paths remain temporary. No Python or protected files changed.

Verification: the publication CLI suite passed all 56 tests, the deployment
unittest suite passed all 16 tests, and the report CLI suite passed all 5 tests.
JavaScript syntax and diff whitespace checks passed.

No production task is newly complete. The authentic deployment audit and
production database are absent, and observation must wait until both 2026-11-08
and fourteen days after verified deployment. The named Anaconda interpreter is
absent and the available Python lacks pytest; that acceptance check remains
unverified. Posting the blocker comment and adding `needs-human` to PR #1196
were rejected with `MCP tool call requires approval, but approval policy is never`.

The checkout's read-only Git index rejected staging. The tested changes are
retained in `/tmp/orchestrator-1175-artifact-history-commit`, with a handoff patch
at `/tmp/orchestrator-1175-artifact-history.patch`.

## Deleted export branch refusal and recovery (2026-11-06)

Reviewed commits `83c8e9c`, `20c80a0`, and `2eec353`: they add provenance
regressions, not a verified production receipt. Both conditional helper/reporting
tasks are satisfied because the extensions remain absent. Reconciliation of the
duplicate PR-body checkboxes was rejected because the connector requires approval
and this run's approval policy is `never`. The blocker comment and `needs-human`
label were rejected for the same reason. No production checkbox is newly complete.

Added two real CLI cases that delete the export branch on the bare remote while
the verifier still holds its cached origin ref and valid commit objects. A
successful fetch must prune that ref and refuse provenance, both without a receipt
and with an existing receipt. Refusal preserves existing receipt bytes and
modification time. Restoring the branch recovers the original commit, deployment
instant, and audit evidence without republishing. All state remains temporary.

Verification: 58 publication CLI tests, 16 deployment unittests, and 5 report CLI
tests passed; JavaScript syntax and diff whitespace checks passed. No Python or
protected files changed. The required Anaconda pytest command cannot start because
`/opt/anaconda3/bin/python3` is absent. Production provenance and database access
remain unavailable; observation must wait until both 2026-11-08 and fourteen days
after verified deployment.

The checkout's read-only Git index rejected staging. The tested changes are
retained in `/tmp/orchestrator-1175-deleted-branch-commit`, with a handoff patch
at `/tmp/orchestrator-1175-deleted-branch.patch`.

## Rewritten export history refusal and recovery (2026-11-06)

Added four real CLI cases for a branch rewound to an unmarked commit or replaced
with a different marked commit containing the identical filter artifact, each
with and without a receipt. Verification must fetch the changed branch and reject
the displaced original commit even though its objects remain available locally.
An audit naming the replacement commit must also fail when an original receipt
already exists. Restoring the original history recovers the original commit,
deployment instant, and evidence without changing retained receipt bytes or
modification time. All Git repositories, home directories, and state are temporary.

Verification: 62 publication CLI tests, 16 deployment unittests, and 5 report CLI
tests passed. JavaScript syntax and diff whitespace checks passed. No Python or
protected files changed; the Python formatting gate does not apply to these edits.

No production checkbox is newly complete. The production database and verified
deployment audit remain unavailable. Observation must wait until both 2026-11-08
and fourteen days after verified deployment. The required Anaconda pytest command
could not start because `/opt/anaconda3/bin/python3` is absent. PR #1196 is open
and ready for review. The connector rejected the blocker comment and `needs-human`
label with `MCP tool call requires approval, but approval policy is never`.

The checkout's read-only Git index rejected staging. The tested changes are
retained in `/tmp/orchestrator-1175-rewritten-history-commit`, with a handoff patch
at `/tmp/orchestrator-1175-rewritten-history.patch`.

## Malformed deployment timestamp CLI regression (2026-11-06)

Reviewed commits `17a54f2` through `24825c6`. They do not supply a verified
production publication receipt. The first publication task remains incomplete;
the conditional helper/reporting tasks are satisfied because both extensions
remain absent. Updating the duplicate PR-body checkboxes, posting the blocker,
and adding `needs-human` were rejected with
`MCP tool call requires approval, but approval policy is never`.

Added eight real CLI cases rejecting null, boolean, numeric, invalid-calendar,
non-UTC, fractional-second, epoch-zero, and pre-epoch deployment timestamps.
Each runs both without and with an existing receipt and proves refusal occurs
before Git or database access. All invalid-provenance cases now also verify that
an existing receipt's modification time remains unchanged, alongside its bytes.
All state remains temporary. No Python or protected files changed.

Verification: 70 publication CLI tests, 16 deployment unittests, and 5 report CLI
tests passed; JavaScript syntax and diff whitespace checks passed. The required
Anaconda pytest command cannot start because its interpreter is absent. The
production database and deployment audit remain unavailable, and observation
must wait until both 2026-11-08 and fourteen days after verified deployment.
No production checkbox is newly complete.

The checkout's read-only Git index rejected staging. The changes are retained
in `/tmp/orchestrator-1175-timestamps-commit-UVJPat`, with a handoff patch at
`/tmp/orchestrator-1175-timestamps.patch`.

## Published artifact timestamp contract regression (2026-11-06)

Added three isolated CLI cases rejecting a published `generated_at` with no
timezone, a non-UTC offset, or fractional seconds. Each uses a real bare remote
and its own history, then verifies that neither the malformed first publication
nor a later repaired artifact establishes provenance. Refusal preserves existing
receipt bytes and modification time, leaves the remote tip unchanged, and keeps
the temporary home and feedback database unused.

Verification: all 73 publication CLI tests, 16 deployment unittests, and 5 report
CLI tests passed. All three new cases fail against a disposable exporter copy
that skips UTC/whole-second validation only for `generated_at`. JavaScript syntax
and diff whitespace checks passed. No Python or protected files changed.

No task checkbox is newly complete. The verified production publication audit
and production database remain unavailable; observation must wait until both
2026-11-08 and fourteen days after verified deployment. The named Anaconda
interpreter is absent, and available Python lacks pytest. PR #1196 is open and
ready for review. Posting the blocker comment and adding `needs-human` were
rejected with `MCP tool call requires approval, but approval policy is never`.

The checkout's read-only Git index rejected staging. The tested changes are
retained in `/tmp/orchestrator-1175-generation-provenance-commit`, with a handoff
patch at `/tmp/orchestrator-1175-generation-provenance.patch`.

## Commit and audit-reference input regression (2026-11-06)

Reviewed commits `17a54f2` through `7f27ce5`. No verified production deployment
receipt was added; both publication checkboxes must remain unchecked. The optional
hosted-pass helpers and reporting remain absent. PR-body reconciliation, the
blocker comment, and `needs-human` label were rejected with `MCP tool call requires
approval, but approval policy is never`. PR #1196 is open and ready for review.

Added eleven CLI cases rejecting null, numeric, array, object, and non-hex commit
identifiers, plus non-string audit references. Every case checks refusal before
Git access, with and without a saved receipt, preserving its bytes and modification
time and leaving temporary home directories and feedback databases unused. Six
selected cases fail against a disposable copy with weakened SHA and audit-reference
validation, confirming they detect those regressions. No Python or protected files
changed.

The production audit and database remain unavailable, as do pytest and the named
Anaconda interpreter. Observation must wait until both 2026-11-08 and fourteen days
after verified deployment. No additional production task is marked complete.

Verification: 73 baseline and 84 updated publication CLI tests passed, along with
16 deployment unittests and 5 report CLI tests. JavaScript syntax and diff whitespace
checks passed. The read-only Git index prevents staging in the main checkout;
the tested changes are retained in
`/tmp/orchestrator-1175-provenance-types-commit-gh5zv203`, with a handoff patch at
`/tmp/orchestrator-1175-provenance-types.patch`.

## Explicit filter-marker verification (2026-11-06)

The export branch's latest artifact-changing commit is
[`2145116d6046609f5b9e5068a7c226642303ccaf`](https://github.com/stranske/Orchestrator/commit/2145116d6046609f5b9e5068a7c226642303ccaf).
Reading `config/route-weights.json` at that exact commit confirms
`generated_at = 2026-10-24T12:52:31Z`, no `exploration.challenger_filter`, and
`gemini` challengers. This artifact cannot establish the required deployment;
its generation and commit clocks are not publication audit evidence.

Extended the real merge/publication CLI regression with ten cases. Even a valid
challenger map cannot prove deployment when its filter marker is missing, null,
an object, an unknown version, or a version with an unverified suffix. After the
first explicitly marked publication, claiming any earlier unverified commit must
preserve receipt bytes and modification time. Reusing the verified audit recovers
the same receipt without republishing. All repositories, home, state, and database
paths remain temporary. No Python or protected files changed.

Verification: 94 publication CLI tests, 16 deployment unittests, and 5 report CLI
tests passed; JavaScript syntax and diff whitespace checks passed. Disposable
copies that infer the filter from challenger values or accept a version prefix
fail the new missing-marker and version-suffix cases, respectively.

No production task is newly complete. The authentic deployment audit and production
database remain unavailable. Observation requires both 2026-11-08 and fourteen
days after verified deployment. The named Anaconda pytest command cannot start
because its interpreter is absent. PR #1196 is open and ready for review. The
connector rejected both the blocker comment and `needs-human` label with
`MCP tool call requires approval, but approval policy is never`.

The checkout's read-only Git index rejected staging. The tested changes are
retained in `/tmp/orchestrator-1175-filter-marker-commit`, with a handoff patch at
`/tmp/orchestrator-1175-filter-marker.patch`.

## Historical challenger contract regression (2026-11-07)

Extended the real publication-history CLI fixture with codex, cursor, claude,
and null challengers. Four new cases verify the first publication after current
router lanes disappear or become reserved, backup-only, or hosted-unavailable,
both with and without a retained receipt. Validation preserves the historical
v1 contract, commit, deployment instant, audit evidence, receipt bytes, and
modification time. A later publication remains invalid as the first deployment;
retrying the original audit recovers without republishing. All state is temporary.

The focused regression passes all five tests, including its four new cases.
Two disposable exporter mutations fail the new cases: using current router
membership and subtracting current hosted exclusions from the historical contract.
The baseline publication CLI suite passed 94 tests, the deployment unittest suite
passed 16, and the report CLI suite passed 5. The updated publication CLI suite
passed all 98 tests; JavaScript syntax and diff whitespace checks also passed.
No Python or protected files changed.

The live export artifact (blob `e119f98ad8b61e3c7f62a78d4af20e1e182d8135`)
still has `generated_at = 2026-10-24T12:52:31Z`, no filter marker, and Gemini
challengers. No production task is newly complete. The authentic deployment
audit and production feedback database remain unavailable; observation requires
both 2026-11-08 and fourteen days after verified deployment. The named Anaconda
pytest command cannot start because its interpreter is absent. Available Python
lacks pytest and Black; installation failed because PyPI DNS could not resolve.

PR #1196 is open and ready for review. The connector rejected the blocker comment
and `needs-human` label with `MCP tool call requires approval, but approval policy
is never`. The checkout's read-only Git index rejected staging. The changes are
retained in `/tmp/orchestrator-1175-historical-contract-commit`, with a handoff
patch at `/tmp/orchestrator-1175-historical-contract.patch`.


## Later malformed publication regression (2026-11-07)

Reviewed commits `17a54f2` through `b6e8fe5` before continuing. They add isolated
provenance regressions but retain no verified production deployment receipt. Both
production tasks remain unchecked. Hosted-pass helpers and reporting remain
absent. The unchecked conditional instrumentation acceptance item in Full Issue
Text should match its checked Automated Status Summary counterpart; attempting
that PR-body reconciliation was rejected with `MCP tool call requires approval,
but approval policy is never`. The blocker comment and `needs-human` label were
rejected for the same reason. PR #1196 remains open and ready for review.

Added six real publication-history CLI cases. A later marked artifact with a
Gemini challenger or invalid generation timestamp, followed by a repair, must
not invalidate or reset the first valid publication. With and without saved state,
the CLI refuses the later commit, recovers the original audit and timestamp,
preserves receipt bytes and modification time on retries, and does not republish,
create a feedback database, or use the live home directory. All state is temporary.
Removing the verifier's stop at the first marked publication in a disposable
source copy makes the new malformed-challenger cases fail.

The live export blob remains `e119f98ad8b61e3c7f62a78d4af20e1e182d8135`, with
`generated_at = 2026-10-24T12:52:31Z`, Gemini challengers, and no filter marker.
This cannot establish production deployment. The authentic publication audit and
production feedback database are unavailable. Observation must occur on or after
both 2026-11-08 and fourteen days after a verified deployment instant. No new
production task is marked complete. The named Anaconda interpreter, pytest, and
Black are unavailable; this round changes JavaScript tests and this evidence note
only. The checkout's read-only Git index prevents a main-checkout commit.

Verification: the baseline publication CLI suite passed 98 tests and the updated
suite passed all 104 tests. Deployment unittests passed all 16 tests, report CLI
tests passed all 5 tests, and JavaScript syntax and diff whitespace checks passed.
The tested changes are committed in `/tmp/orchestrator-1175-later-contract-commit-r5bs0z2z` with a handoff patch at
`/tmp/orchestrator-1175-later-contract.patch`; the main working tree retains the same edits.
