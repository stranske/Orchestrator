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
