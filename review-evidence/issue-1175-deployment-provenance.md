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
