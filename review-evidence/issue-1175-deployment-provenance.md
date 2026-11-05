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
