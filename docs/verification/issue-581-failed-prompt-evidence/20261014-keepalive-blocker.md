# Keepalive validation blocker — 2026-10-14

The source retention changes and named regression already exist at `fc0414a`.
This round extends existing tests for null evidence paths on valid CLI items,
zero-byte raw output retention, and exact withheld-body retention with no published
file after recording failure. No test nodes or collection/skip ceilings changed.

The roles selftest, Python compilation, whitespace checks, and independent direct
production checks pass. These do not replace the required pytest and Black gates.
Neither available Python has pytest, and Black is absent. Package installation
failed because package-index DNS/network access is unavailable; neither tool is
in the local pip cache. No commit or push was made because the pre-commit Black
gate cannot pass. No task checkbox was marked complete this round.

Attempts to post the blocker comment and add `needs-human` to PR #589 were denied:
`MCP tool call requires approval, but approval policy is never`.
The PR was read successfully and is open with `draft=false`.

Follow-up: provision pytest and Black, run the required focused suite and
repository-wide Black check, then commit the prepared test changes. The next real
role-authored batch of at least five bodies and its qualification query remain
pending; no production batch or 2026-10-27 acceptance is claimed.

## Follow-up at d289b62

Reviewed the four implementation/test commits through `d289b62`. The production
`roles.py` SHA256 still equals the mutation evidence's corrected hash. Committed
logs verify the original named regression, 25-test suite, and both deliberate-break
and restoration controls. Those three local acceptance checkboxes should be checked;
the checked real-batch task must be reopened. The reconciled PR body is prepared at
`/tmp/589-reconciled-pr-body.md`; its update was denied by the connector approval policy.
Posting the blocker comment and adding `needs-human` were also denied.

The named regression now checks exact validator and parse-failure reasons and the
complete set of published/evidence files, including absence of unlisted output for
valid items. A direct production mixed-batch check, roles selftest, compilation and
whitespace checks pass. Pytest remains unavailable in every installed interpreter;
tool installation attempts failed, and both required Black commands exit 127.
These changes remain uncommitted because the required formatting gate cannot run.
No fresh pytest pass or production qualification is claimed. PR #589 remains open
with `draft=false`; its task checkboxes are unchanged because remote writes are denied.

## Follow-up at a405ad3

Reviewed all six PR commits and confirmed the production source still matches the
corrected mutation-proof SHA256. The named mixed-batch test now executes the exact
source-issue qualification query in a fresh subprocess against the saved manifest;
it requires stdout `3 3` and no stderr. No collection nodes were added.

An independent production mixed-batch check with a private Brain passes, including
exact retained bytes and output filenames; its subprocess query prints `3 3`.
Roles selftest, Python compilation and whitespace checks pass. These are controlled
checks, not a pytest pass or the required real role-authored production batch.

Pytest remains absent, package installation fails because DNS is unavailable, and
both mandatory Black commands exit 127. The test change remains uncommitted because
the formatting gate cannot run. PR-body reconciliation, blocker comment and
`needs-human` label were each rejected because connector writes require approval
while this run's approval policy is `never`. The prepared reconciliation is at
`/tmp/589-reconciled-pr-body.md`; PR #589 is open with `draft=false`.

## Follow-up at 5a4132d

The named regression now checks every persisted body and raw-output artifact's
exact UTF-8 bytes in a fresh subprocess. Counting manifest paths alone would miss
an artifact that disappeared after authoring. A direct five-item production replay
passes this same subprocess check; deleting `002.failed.md` makes it fail with
`FileNotFoundError`. No new test nodes or collection/skip ceilings were introduced.
The roles selftest and whitespace check pass.

Both focused pytest commands exit 1 because pytest is absent. Both required Black
commands exit 127 because Black is absent. Local installation attempts cannot find
available distributions. The mandatory formatting gate prevents committing or
pushing the prepared Python change. No fresh pytest pass, deliberate-break pytest
run, completed acceptance checkbox, or real production qualification is claimed.

The connector rejected posting the blocker comment and adding `needs-human` with
`MCP tool call requires approval, but approval policy is never`. PR #589 was read
and remains open with `draft=false`. Its checked real-batch task still needs
reopening until the actual post-merge batch and required program check are verified.

## Follow-up at 27dc63f

Reviewed the implementation, subsequent test commits and committed mutation logs.
The production source still matches the corrected mutation SHA256. The first three
local acceptance criteria have historical verification; the checked post-merge
real-batch task has no supporting production evidence and must be reopened. The
complete proposed PR body is now saved in `589-reconciled-pr-body.md` beside this
note. Updating PR #589 was rejected: `MCP tool call requires approval, but approval
policy is never`.

The named regression now verifies persisted batch/output identity, item targets,
and the original, unique role-run IDs, so retained artifacts remain attributable.
An isolated five-item production replay passes these assertions and exact retained
byte checks; wrong-target and duplicate-run negative controls are detected. This
does not substitute for pytest or a real role-authored batch.

Focused pytest exits 1 because pytest is absent. Both mandatory Black commands
exit 127 because Black is absent; package installation also fails. The Python
change must remain uncommitted until the required formatting gate passes. No new
acceptance completion, deployment, or 2026-10-27 qualification is claimed.
