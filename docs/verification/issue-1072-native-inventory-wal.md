# Native inventory WAL verification

Checked on 2026-11-03 against checkout `5bbc39549a51044692eb74910855a66799625e1e`.
The new `tests/test_adjudicator_native_inventory.js` exercises the real Python
inventory CLI and native journal producer through isolated temporary fixtures.
It verifies committed receipts still in SQLite WAL, separation of uncommitted
changes from a later committed observation, and refusal of dispatch/retry modes.
Original recursive paid identities, unresolved UNKNOWNs, foreign receipts and
untrusted canonical grade claims remain separately visible. Report, database
and WAL hashes remain unchanged by inventory; no Brain/state store is created.
The main-file-only control contains zero attempts while the live journal has
three, confirming that this test actually exercises WAL-resident evidence.

Validation:

- `node --test --test-isolation=none --test-reporter=tap tests/test_adjudicator_inventory.js tests/test_adjudicator_native_inventory.js`: 29 passed.
- `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_adjudicator_diagnostics.py -q`: 32 passed.
- The same unittest command with `test_adjudicator_replay_revision.py`: seven passed.
- `node --check tests/test_adjudicator_native_inventory.js` and `git diff --check`: passed.

These are fixture results, not production observations. The configured canonical
report, independent outcomes database and default execution mirror are absent
in this runner. No authentic original/journal inputs were supplied. No dispatch,
canonical refresh, October 18 proof or next-20 observation is claimed, and no
operational checkbox is complete. Pytest and Black are unavailable; installation
failed. This change edits no Python files and does not establish the required
full `tests/test_adjudicator_retro.py` result.

Local staging failed because `.git/index.lock` cannot be created on the read-only
Git mount. Committing the verified JavaScript file through GitHub's contents API
was also rejected because it requires approval and this run's policy is `never`.
No commit or push was made. PR #1081 was observed open with `draft=false`.

## Deployment and mirror CLI verification

Follow-up checked on 2026-11-03 against `afb27ee3affc11779d720558a6973f9b78f6befa`.
Two additional JavaScript integration tests exercise the real Python inventory
CLI with separate Git deployment and mirror fixtures. A flat mirror generation
inside the deployment checkout retains its selected/resolved paths and module
hashes without inheriting the parent revision. A changed deployment validator
updates its hashes and dirty status while the mirror and native receipts remain
unchanged. A dirty linked mirror worktree retains its own revision and hashes
despite inherited deployment Git locations. Neither observation grants a live
reader binding, historical proof, canonical grade authority or dispatch permission.

The inventory JavaScript command above now passes all 31 tests. The diagnostic
unittest command still passes all 32 tests; JavaScript syntax and `git diff --check`
pass. No Python files changed. The full retrospective pytest command with
`-m "not slow"` cannot start because pytest is absent in every installed Python
interpreter. The configured canonical report and independent outcomes database
are still absent. No authentic original/journal or runtime bindings were supplied.
No dispatch, historical observation, canonical refresh or fixed next-20 decision
is claimed, and reconciliation found no completed operational checkbox.

Staging the test change failed on the read-only `.git/index.lock` location; no
commit or push was made. Automatic approval review rejected PR-body reconciliation,
the `needs-human` label and the blocker comment because those tools require
approval and this run's policy is `never`. PR #1081 was rechecked open with
`draft=false` at the revision above.

## Commit during inventory verification

Follow-up checked on 2026-11-03 against `eb9cb496f103f80ea09126c7dc7038a2edff3a9e`.
Two new tests commit native producer transitions after inventory's initial receipt
read: a reservation acquires its paid invocation identity, or an in-flight attempt
acquires a raw receipt. Hashes captured immediately after each commit establish
that the changes reside in WAL while the main database remains unchanged.
The real inventory CLI rejects both mixed observations without emitting JSON.
A later inventory retains the committed identities/receipt hashes, original paid
history and UNKNOWN boundary without authenticating grades or authorizing dispatch.
Producer connection cleanup may checkpoint afterward; that resulting durable
state is checked separately from the commit-time WAL observation.

The inventory JavaScript command above passes all 33 tests, the diagnostic
unittest command passes 32, and the replay/publication unittest command passes
seven. JavaScript syntax and `git diff --check` pass. Removing only the final
journal-record comparison in a temporary source copy makes both new tests fail
with an unexpected successful CLI exit. No repository Python file changed.

Reconciliation found no completed operational task. The configured canonical
report, runtime directory and independent outcomes database remain absent. No
authentic production inventory, dispatch, canonical refresh, October 18 evidence
or next-20 observation is claimed. Pytest and Black remain unavailable. Staging
failed because `.git/index.lock` is read-only, so no commit or push was made.
Automatic approval review rejected PR-body reconciliation, `needs-human` labeling
and the blocker comment because those tools require approval and this run's policy
is `never`. PR #1081 was rechecked open with `draft=false` on the revision above.

## Validation committed during inventory

Follow-up checked on 2026-11-03 against checkout
`22bc62101ce320c7cb56430b12d2867b0d8bdbd4`. The native inventory integration
test now also commits validation of a synthetic binary receipt after inventory's
initial journal read. The real native producer performs reservation, invocation,
raw persistence and validation; the role validator checks the fixture proposal.
Commit-time hashes prove validation resides in WAL without a main-file change.
The CLI rejects the mixed observation without JSON, and a fresh inventory
retains the raw and validated receipt hashes and all original paid identities.
The binary receipt still requires fresh authentication; retained canonical grade
claims remain unauthenticated and dispatch remains unauthorized.

Validation: the combined inventory JavaScript command above passes all 34 tests;
the diagnostic unittest command passes 32 and the replay/publication command
passes seven. JavaScript syntax and `git diff --check` pass.

The production canonical report, outcomes database and configured runtime are
absent in this runner. No authentic original or journal location was supplied.
The full retrospective pytest command with `-m "not slow"` cannot start because
pytest is unavailable. No Python files changed. Staging failed because
`.git/index.lock` is read-only, so no commit or push was made. GitHub access also
failed, so PR readiness could not be rechecked during this follow-up. No
operational task checkbox is complete and no production observation is claimed.

## Earlier terminal receipt before a later UNKNOWN

Checked on 2026-11-03 against checkout
`680de345d35f209a5aafde39387519b471dda441`. Two new integration cases use
the real native producer to retain a paid abstention or validator rejection,
then begin an invocation at a different material revision without a response.
The inventory CLI preserves both receipts, recursive original paid identities,
and the later UNKNOWN locator. It changes no report/database/WAL bytes, exposes
no response or validator payload, and grants no grade or dispatch authority.
These are isolated fixtures, not authentic production observations.

Validation: both inventory JavaScript suites pass all 36 tests; diagnostic
unittests pass 32 and replay/publication unittests pass seven. JavaScript syntax
and `git diff --check` pass. In a temporary source copy, making an earlier
validated receipt hide the unresolved attempt causes both new tests to fail.
No repository Python files changed. The full retrospective pytest command with
`-m "not slow"` remains blocked by missing pytest; package installation failed.

The configured canonical report, outcomes database, runtime and default mirror
are absent. No original report/journal locations or live reader bindings were
supplied. No operational task is complete. Local staging failed on the read-only
Git metadata; automatic approval review rejected the connected GitHub file
commit because approval is required and this run's policy is `never`. No commit
or push was made. The same approval policy rejected the blocker comment and
`needs-human` label. PR #1081 was rechecked open with `draft=false` at the
revision above.

## Mixed original-row first-break CLI verification

Checked on 2026-11-04 against checkout
`826e318f9bf94da418ebc7284607809365d27774`. A new native inventory integration
test supplies nine original rows covering a decided row, missing packet,
substantive-evidence gap, invalid proposal, discarded proposal with a recorded
validator rejection, absent later truth, abstention, provider error and proposal
mismatch. The real CLI retains each row's first break, original decision,
recursive paid history and explicit UNKNOWN reservation. A native producer's
terminal binary receipt and a retained canonical projection cannot supply the
missing independent truth. Report, database and WAL hashes remain unchanged.

Validation: the combined inventory JavaScript suites pass all 43 tests;
diagnostic unittests pass 32 and replay/publication unittests pass seven.
JavaScript syntax and `git diff --check` pass. Two independent temporary-source
mutations, skipping substantive-evidence diagnosis or independent-truth diagnosis,
both cause the new regression to fail. No repository Python files changed.

These are fixture results. The configured canonical report, outcomes database,
runtime and default mirror are absent; authentic original report and journal
locations remain unavailable. The full retrospective pytest command with
`-m "not slow"` cannot start because pytest is unavailable. No dispatch, canonical
refresh, historical proof or live-shadow decision is claimed; no operational
checkbox is complete.

Staging failed because `.git/index.lock` is on a read-only filesystem. Automatic
approval review rejected the connected GitHub file commit, blocker comment and
`needs-human` label because approval is required and this run's policy is `never`.
No commit or push was made. PR #1081 was observed open with `draft=false` at the
checkout revision above.

## Incomplete substantive evidence across original rows

Checked on 2026-11-04 against checkout
`27723244f2619d03a6b3407f5a2a23546305f120`. A new real-CLI regression covers
23 original rows: 21 incomplete patch/criteria combinations and two complete
controls. Missing, blank, malformed, unrevisioned and truncated evidence remains
the first retained break despite completeness/qualification claims. Both binary
decisions, later-truth labels, original and earlier paid identities, and an
unresolved reservation remain separately visible. Inventory leaves the original,
canonical report, database and WAL unchanged and exposes no private evidence.

Validation: the two inventory JavaScript suites pass all 44 tests; diagnostic
unittests pass 32 and replay/publication unittests pass seven. JavaScript syntax
and `git diff --check` pass. In separate temporary source copies, accepting a
truncated patch or replacing the all-criteria check with any-criterion acceptance
each makes the new regression fail. No repository Python files changed.

These remain fixture results. The configured canonical report, independent
outcomes database and default execution mirror are absent. Authentic original
report/journal locations and live reader bindings were not supplied. The full
retrospective pytest command with `-m "not slow"` cannot start because pytest is
unavailable; package installation also failed. No production diagnosis, dispatch,
canonical refresh, October 18 proof or next-20 decision is claimed. No operational
checkbox is complete.

Staging failed because Git metadata is read-only. Automatic approval review
rejected the connected GitHub file commit because it requires approval and this
run's policy is `never`. The same review rejected the blocker comment and
`needs-human` label. No commit or push was made. PR #1081 was rechecked open
with `draft=false` at the checkout revision above.

## Saved proposal contract failures before missing truth

Checked on 2026-11-04 against checkout
`5685a7027804d3fed1adc2baf8b17665340054f7`. A new real-CLI regression
exercises 20 saved proposal contract failures, including malformed decisions,
references and evidence assessments, forbidden terminal keys and conditional
mutation commands. Three valid proposal controls reach the independent-truth
break or abstention instead. All rows lack later truth; validator failures must
remain the earlier break. Decisions, recursive paid histories and the native
UNKNOWN reservation survive, while private evidence and proposals stay absent
from diagnostics. Original, canonical, journal and WAL hashes remain unchanged.

Validation: the combined inventory JavaScript suites pass all 45 tests;
diagnostic unittests pass 32 and replay/publication unittests pass seven.
JavaScript syntax and `git diff --check` pass. Removing only the diagnostic
proposal-validator branch in a temporary source copy makes the new test fail,
reporting missing later truth instead of the required validator failure.
No repository Python files changed.

These are fixture results. The configured canonical report, independent outcomes
database and runtime are absent. Authentic original report/journal locations
and live reader bindings remain unavailable. The full retrospective pytest
command with `-m "not slow"` cannot start because pytest is absent; package
installation fails because PyPI DNS resolution is unavailable. No production
diagnosis, dispatch, canonical refresh, October 18 proof or next-20 observation
is claimed, and no operational checkbox is complete.

Staging fails on the read-only `.git/index.lock` location. Automatic approval
review rejects the connected GitHub file commit, blocker comment and
`needs-human` label because approval is required and policy is `never`.
No commit or push was made.

## Specific retained evidence locations

Checked on 2026-11-04 against checkout
`5ced88edf88e0f4dd90e5d18b80d999d27d918a2`. Missing substantive evidence now
has a `retained_evidence_gap` with its original-row field reference and a fixed
reason. The evidence floor and diagnostic share the same predicate. Patch shape,
text, revision and truncation are checked before the ordered acceptance criteria;
complete evidence has no gap. No evidence contents are included in this field.

The real inventory CLI regression checks 25 evidence cases, including simultaneous
patch/criteria failures and failures in multiple criteria. Its mixed-cohort test
also checks that a missing patch remains the first break before a recorded
validator rejection. Decisions, independent-truth claims, original paid history
and UNKNOWN reservations remain retained; original, canonical, database and WAL
bytes remain unchanged. Running the evidence regression against the original
source fails because the field reference and reason are absent.

These are fixture results. Current checks find the default canonical report,
independent outcomes database and runtime absent. Authentic original report and
journal locations remain unavailable, so production diagnosis, retries,
publication and live-shadow observations remain unverified and unchecked.
Black and pytest are unavailable; package installation failed. The required Black
format/check commands and full retrospective pytest command cannot run. No Python
changes may be committed until the required formatting gate passes.

Validation: all 45 inventory integration tests, 32 diagnostic unittests and seven
replay/publication unittests pass. JavaScript syntax and `git diff --check` pass.
No commit or push was made. Automatic approval review rejected the blocker
comment and `needs-human` label because they require approval and this run's
policy is `never`.

## Native receipt lifecycle and original first breaks

Checked on 2026-11-05 against checkout
`4ecbb19745f577feae14c557b5140727260e2f6e`. Two new real-CLI regressions
observe matching-source and foreign-source attempts through native reservation,
invocation, raw persistence and validation. A retained binary decision with no
independent later truth keeps that first break at every stage. Unresolved
attempts remain UNKNOWN, including foreign attempts. A terminal validator
rejection requires receipt review and cannot supply the original's missing truth.
Recursive original paid identities remain visible; overlap with native identities
is accounted for without counting the same backend identity twice in the union.
Reports, main database and persisted WAL bytes remain unchanged by inventory;
SQLite may create an empty WAL after producer cleanup has checkpointed it.

Both inventory JavaScript suites pass all 48 tests. Independently filtering foreign attempts out
of diagnostics or ignoring missing later truth in temporary source copies makes
the new regression fail. The 32 diagnostic and seven replay/publication unittest
cases also pass. JavaScript syntax and `git diff --check` pass. No Python files
changed. The full retrospective pytest command with `-m "not slow"` cannot start
because pytest is unavailable; installing pytest and Black failed.

These are fixture observations. The configured canonical report at
`/home/runner/.codex/orchestrator/capability-program/adjudicator-retro.json`,
independent outcomes database at
`/home/runner/.codex/orchestrator/feedback/orchestrator.db` and runtime directory
remain absent. Authentic original/journal locations and live reader bindings
remain unavailable. No production diagnosis, retry, publication, October 18
proof or next-20 decision is claimed; no operational checkbox is completed.
