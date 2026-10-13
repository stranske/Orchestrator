# Issue #569: research ownership recovery

## Persisted legacy-feed intake validation, based on `84915db`

- [x] `priority:high` + `agents:paused` contributes zero eligible subjects.
- [x] `priority:high` + `agent:codex` contributes zero eligible subjects.
- [x] The auto-pilot pause exclusion and eligible unowned control still pass.
- [ ] Format, run original pytest suites, commit/push and complete guarded delivery.

The existing paused/assigned regression tests now also load a fresh persisted feed from an
older producer that admits the excluded issue and reports two eligible subjects. Production
intake rejects that issue with `owned_or_paused` and schedules only its unowned neighbour.
Removing the scheduler ownership guard in a temporary source copy produces two assertion
failures at the new intake assertion; current production passes all 23 ownership/feed/wiring
unittest cases. All four affected module selftests pass. Existing test methods are extended;
the collection floor, ceilings, exemptions, workflows and launch settings are unchanged.

Changes remain **uncommitted**: Black and pytest are missing, installation failed, both
mandatory Black commands fail with `command not found`, and targeted pytest fails with
`No module named pytest`. The mandatory formatting gate prevents a Python commit or push.
No research, Brain, mirror, dispatch or deployment activation occurred.

PR570 is open and ready at `84915dbd1bd8b6793c289375915890077a21dac8`; connector readback
returns three resolved review threads and no active unresolved threads. GitHub CLI cannot
connect; the merge-guard dry-run blocks on `review-thread GraphQL query failed`. No new head,
complete expected-check verification, seven-minute delivery anchor or actual `verify:compare`
verdict was obtained. The checked terminal-delivery criterion remains unsupported by this run.
Automatic approval rejected the verified task-checkbox reconciliation, blocker comment and
`needs-human` label because each requires approval while policy is `never`; none occurred.

## Persisted discovery validation, based on `fa48d82`

Recent commits were reviewed before extending the regressions. Both reported defects are
already corrected in production and verified in this run:

- [x] `priority:high` + `agents:paused` contributes zero eligible subjects.
- [x] `priority:high` + `agent:codex` contributes zero eligible subjects.
- [x] The auto-pilot pause exclusion and eligible unowned control still pass.
- [ ] Complete required formatting, pytest validation, commit/push and terminal delivery.

The existing paused/assigned tests now exercise strict GitHub discovery with mocked responses,
persist the production feed as JSON, load it through `tick.load_research_feed`, and pass its
items to the production scheduler. Each case includes an eligible unowned neighbour, verifies
the ownership counts, and admits only that neighbour. Existing test methods were extended;
the collection floor, ceilings, exemptions and workflows are unchanged.

All 23 ownership/feed-validation/wiring unittest cases and all four affected module selftests
pass. A fresh four-case reproduction against the exact original production module at
`4a3dcf25addce6399d3267df42fb3b7063150651` has two assertion failures (`eligible=1`) for
the reported labels and two passing controls; current production passes all four. The original
module's Git blob was verified as `5a824ddd63cefee8390dcbd28a8fee91704348b9`.

The test changes remain **uncommitted**. Black and pytest are missing; installation in a
temporary virtual environment failed. Both required Black commands return `command not found`,
and targeted pytest returns `No module named pytest`. The mandatory Black gate prohibits a
Python commit or push. Original pytest suites were not rerun, and no new review-floor anchor
exists. Research launching remains off; no live Brain, mirror, dispatch or deployment was used.

Connector readback at existing head `fa48d82816f8f9384f818d00f133110ce79372a4` confirms PR570
is open and ready, all 60 returned check runs are success/skipped, all 16 suites were read
(seven queued), and there are zero active unresolved review threads. The expected-check
reporter cannot connect through `gh`; merge-guard dry-run blocks on `review-thread GraphQL query
failed`. No actual `verify:compare` verdict appears in the read checks, statuses or comments;
the automated completion checkpoint is not such a verdict. Terminal delivery is unverified.

PR-body reconciliation was attempted to check both reproduced tasks and uncheck the unsupported
terminal-delivery criterion. Automatic approval rejected the body update, `needs-human`
label and blocker comment because approval policy is `never`; none occurred. The verified local
checkboxes above record the reconciliation pending an authorized remote update.

## Discovery ownership validation, 2026-10-13, based on `3695464`

The two unchecked reproduction tasks are already fixed in current production:

- [x] `priority:high` + `agents:paused` contributes zero eligible subjects.
- [x] `priority:high` + `agent:codex` contributes zero eligible subjects.
- [x] The auto-pilot pause exclusion and eligible unowned control still pass.

This round extends the existing paused-issue regression through production discovery and
scheduler intake. Before correction, unreadable discovery labels yielded five incorrect results
and two crashes. Discovery now validates the original label list before `_labels` can coerce
unknown ownership into strings. Invalid lists use the incumbent dispatch unknown-ownership
refusal; a readable unowned neighbour remains schedulable. Raw local RED/GREEN outputs are in
`20261013-discovery-labels/`. All 23 ownership/feed-validation/wiring unittest cases and all four
affected module selftests pass. Existing test methods were extended; collection floor, ceilings,
exemptions and workflow files are unchanged. Research launching remains off.

The source/test changes are **uncommitted**. Black and pytest are unavailable, and installing
them into a temporary virtual environment failed because `pypi.org` cannot resolve. Both mandated
Black commands failed with `command not found`; original intake/control/usage/followup targeted
pytest failed with `No module named pytest`. The required formatting gate prevents a commit or
push. A runner with Black, pytest and GitHub CLI network access is needed to finish validation,
commit/push and delivery. No Brain, installed mirror, dispatch or deployment activation occurred.

Connector readback confirms PR570 is open and ready at
`36954643ba714fdc959fb3c3e9b57a2d4d32e53a`: all 82 returned check runs are success/skipped;
all 20 suites were read, including seven queued suites; zero active unresolved review threads
were returned. The expected-check reporter cannot connect through `gh`; merge-guard dry-run
blocks on `review-thread GraphQL query failed`. No actual `verify:compare` verdict was found in
the read checks/statuses. These observations apply to the existing head, not the local correction.
No new push/update review-floor anchor exists, and no merge or terminal delivery is claimed.

The proposed PR-body reconciliation checks both reproduced tasks and unchecks the unsupported
terminal-delivery criterion. Automatic approval rejected that update, the blocker comment and
the `needs-human` label because those actions require approval while policy is `never`; none
occurred. This local section records the verified task results and remaining blockers.

## Review follow-up validation, 2026-10-13

Recent commits through `4ce9912` already implement the three concrete exclusions,
the bounded P2 controls and the launch-off gates. All 23 ownership/feed/wiring
unittest cases pass in this run. The verified reconciliation would check the
three tasks and ownership, P2 and launch-off criteria; original pytest suites and
terminal delivery remain unchecked. Automatic approval rejected the PR-body
update, blocker comment and `needs-human` label (approval policy `never`).

This round fixes two additional reproduced review gaps: malformed scheduler
labels caused two exceptions and three incorrect refusal reasons, and wholly
targetless object feeds reported `fresh` in two cases. Both regressions were RED
before correction and GREEN afterward. Unknown labels now use the dispatcher's
ownership refusal, while readable unowned items still reach scheduling beside a
malformed item. Targetless nonempty feeds are unreadable; empty and mixed usable
feeds retain their contracts. Both in-progress spellings now have discovery,
scheduler and launch controls. Existing test methods were extended; collection
floor, skip ceilings and exemptions are unchanged. The original lane fixture
now supplies explicit empty labels without changing its assertion.

All 23 unittest cases and the four affected module selftests pass. Black and
pytest are absent; package installation failed. Both mandated Black commands
failed, so the source/test changes remain **uncommitted**, with no push or new
review-floor anchor. No live Brain, installed mirror, research, dispatch or
deployment was activated.

Existing head `4ce9912fb062e2f7bc778eed16e76de84e35b5b1` remains open and ready.
Connector readback returned all 71 check runs and 18 suites at that head,
including seven queued suites, and three active unresolved review threads.
The expected-check reporter cannot connect through `gh`; merge-guard dry-run
blocks on `review-thread GraphQL query failed`. No actual `verify:compare`
verdict was found in the checks/comments read. No merge or delivery is claimed.
Complete formatting and original pytest validation before committing, then
reconcile checkboxes and perform the full guarded delivery sequence at the
resulting pushed head. Earlier sections below retain historical run evidence.

## Current keepalive validation, 2026-10-13

The existing launch ownership test now carries an eligible unowned production feed through
`research_tick` after the live issue gains each tested pause/ownership label or in-progress status.
The tick rejects the subject before claiming it or preparing any arms. The launch-off test now
mocks the actual production entry point, `prepare_arms`, checks that no claim occurs, and covers
the explicit `ORCH_RESEARCH_ARM=0` case. Heartbeat calls are mocked and the database is in memory.
These extend existing test methods without changing the collection floor, ceilings or exemptions.

All 23 ownership/feed-validation/wiring unittest cases pass; all four affected module selftests
pass. Fresh original-production reproduction uses the exact `backlog.py` fetched at
`4a3dcf25addce6399d3267df42fb3b7063150651`, hash-checked against the existing manifest:
both reported ownership assertions fail with `eligible=1`, the auto-pilot pause control passes,
and current production passes all three assertions. Raw logs and a receipt are in
`20261013-keepalive/`.

The changes remain **uncommitted**. Black and pytest are unavailable in every checked Python
interpreter, dependency installation failed, both required Black commands return `command not
found`, and targeted pytest returns `No module named pytest`. The formatting gate forbids a
Python commit or push until Black passes. The original intake/control/followup pytest suites
have not been rerun in this round. Launching remains off; no live Brain, installed mirror,
dispatch or deployment activation occurred.

PR570 remains open and ready at `22c90ee122f0203f73b40cc649145cb696679c34`. Connector readback
covered all 110 check runs and all 29 suites returned at that head, including queued application
suites and an in-progress Actions suite. The maintained expected-check reporter cannot connect
through `gh`, so complete expected topology remains unknown. The connector returned zero review
threads, but the mandatory merge-guard dry-run independently blocks on `review-thread GraphQL
query failed`; no merge occurred. No actual `verify:compare` verdict was found in read checks,
statuses or comments. These remote facts apply to the existing head, not the local test changes;
there is no new pushed head or verified seven-minute delivery anchor.

Task reconciliation would check the three concrete tasks plus the ownership, bounded P2 and
launch-off acceptance criteria based on verified implementation. The original-suite and final
delivery criteria stay unchecked. Automatic approval rejected the PR-body update, `needs-human`
label and blocker comment because each requires approval while policy is `never`; none occurred.
The next capable run must format, run the original targeted suites, commit/push, reconcile the
checkboxes, and complete the exact-head checks/review-floor/merge-guard/verify:compare sequence.

## Previous keepalive validation, at 22c90ee

The working-tree change strengthens the existing auto-pilot pause regression with a single
production feed containing both reproduced ownership failures, the existing pause control,
and an eligible unowned prioritized issue. Only the unowned issue reaches scheduler intake.
Scheduler rejection now checks normalized case and whitespace for every tested ownership family.
These extend existing test methods; no collection floor, ceiling, exemption or workflow changed.

Verified task results:

- [x] `priority:high` + `agents:paused` contributes zero eligible subjects.
- [x] `priority:high` + `agent:codex` contributes zero eligible subjects.
- [x] `priority:high` + `agents:auto-pilot-pause` remains excluded.

Fresh original-source RED uses `backlog.py` from
`4a3dcf25addce6399d3267df42fb3b7063150651`, with SHA-256
`6ce66422bafff61e015cbfc644443be662cda63eb1ce9f27dcded9ab7b1250f8`.
Both reproduced assertions fail with `eligible=1`; the original auto-pilot pause control stays
`eligible=0`. Current production passes all 23 ownership/feed-validation/wiring unittest cases.
Raw logs are `keepalive-original-red.log` and `keepalive-current-green.log`.

This change is **uncommitted**: Black and pytest are absent, PyPI DNS resolution fails, and both
mandated Black commands fail with `command not found`. The original intake/control/followup
pytest command cannot run here. The Python pre-commit gate prohibits committing or pushing.
`git diff --check` passes; this does not replace Black or pytest.

Remote evidence applies only to existing head
`d5ea3d14577c7faffed2f31c9b02f8e3ba01c993`, not these working-tree changes. PR570 is open and
ready, with no review threads returned. Readback returns 82 check runs and all 20 suites,
including seven queued suites. The hosted verifier log reports 3205 passed, 26 skipped,
97 selftests and 3 of 5 gates passing. The prior maintained expected-check report still names
`auto-pilot`, `format-pause-wakeup` and `gate` as unreported. Complete expected topology has not
been cleared; no actual `verify:compare` verdict was found in checks, statuses or comments.
`merge_guard` dry-run fails closed with `review-thread GraphQL query failed`; no merge occurred.

Automatic approval review rejected updating PR task checkboxes, adding `needs-human`, and
posting the blocker comment because each mutation requires approval while this run's policy
is `never`. Those remote mutations did not occur. Run Black and the relevant pytest suites
in a capable environment before committing; then resolve the existing expected-check blocker,
read every suite at the resulting head, satisfy the seven-minute floor and thread clearance,
use the required active merge guard, and consume actual `verify:compare` evidence.
Launching remains off; no live Brain, installed mirror, dispatch or deployment was activated.

## Current recovery, 2026-10-12

The existing PR is rebased onto merged source PR566, main
`68c79c90008264a3b648f2b8d533c0e8d3995510`. Current main improvements and
all original intake assertions are retained. Discovery and scheduler/launch revalidation
share the incumbent dispatcher ownership policy. Full issue/PR result pages fail closed
as incomplete; malformed and future feed timestamps are rejected. Missing timestamps
retain the existing stale classification and mixed valid/malformed rows retain valid entries.
Launching remains off.

Measured collection is3231, main3208 plus23 independently executed nodes. The affected
suites pass44 tests and93 subtests; all four affected module selftests pass. Repository-wide
Black and Ruff pass. Four deliberate production breaks (issue limit, PR limit, scheduler
ownership and future timestamp) each produce an assertion failure, then pass after
byte-identical restoration. Raw logs, JUnit and hashes are in `20261012-recovery/`.
These are bounded proofs; fresh full hosted verification, complete expected check topology,
exact-head review clearance and the seven-minute push floor remain delivery requirements.
No installed mirror, Brain, live dispatch or deployment was changed.

## Historical evidence, retained verbatim

The following describes earlier attempts and their then-current environment. Its read-only
checkout, missing packages and unmerged source statements are historical, not current.

# Issue #569: local research ownership correction

The ownership correction is present in this branch. The feed-wiring restoration described below
is locally tested but **not committed, pushed, merged, or deployed**. The original evidence recorded
source PR #566 at `4a3dcf25addce6399d3267df42fb3b7063150651` with an active,
non-outdated unresolved P1 thread. Its current review state still requires a fresh read.

## Current restoration round

Verified task progress:

- [x] `priority:high` + `agents:paused` contributes zero eligible subjects.
- [x] `priority:high` + `agent:codex` contributes zero eligible subjects.
- [x] `priority:high` + `agents:auto-pilot-pause` remains excluded.
- [ ] Complete original integration and reconcile the actual collection floor.
- [ ] Run original intake/control/followup suites and full delivery verification together.

`orchestrate.sh` and `ARCHITECTURE.md` are restored byte-for-byte from original #566 commit
`4a3dcf25addce6399d3267df42fb3b7063150651`. This restores feed generation while dispatch is
shadowed, respects deferred GitHub reads, and preserves both owner launch gates. The existing
corrected ownership/freshness/PR completeness code remains in place.

Four new standard-library tests execute only the discovery shell fragment with a fake Python
command. They check shadow intake with the arm flag absent/off/on, deferred reads, failure
handling under pipefail, and coexistence with dispatch discovery. They execute no live tick.
`restoration-wiring-red.log` records all four tests failing before restoration (seven failing
assertions/subtests); `restoration-green.log` records all 22 ownership/feed/wiring tests passing.
`restoration-original-red.log` freshly reproduces the two ownership failures on the exact
original production module, while the auto-pilot pause control passes. All four affected module
selftests pass again; their `restoration-*-selftest.log` files retain the results. Shell syntax,
`git diff --check`, and exact original shell/architecture comparisons pass.

The seven original intake assertions remain intact, but pytest is missing, so their pytest run
and the combined control/followup run could not execute. Black is also missing; both required
commands fail, and package installation cannot find a distribution in this environment. No
Python commit is allowed until the formatting gate passes. Git staging itself fails with
`index.lock: Read-only file system`. `.verify-floor.json` is unchanged: actual pytest collection
could not be measured, and no collection estimate replaces that measurement. All ceilings and
exemptions remain unchanged.

GitHub readback fails with `error connecting to api.github.com`, so current PR readiness,
expected checks, suites, threads, and the actual `verify:compare` verdict remain unverified.
There is no new pushed head or seven-minute anchor. No merge was attempted, and no Brain,
installed mirror, dispatch, deployment, or launch activation occurred. Review `restoration.patch`
for the source/test changes, then complete formatting, measured collection, full verification,
commit/push, ready-PR readback, and the guarded delivery sequence in a capable environment.

## Prior round evidence

`source-correction.patch` applies to that exact source head. It changes discovery, scheduler
intake, launch revalidation, feed validation, and PR discovery completeness; adds 18 regression
tests; and adds an empty labels result to the existing open/closed launch fixture. The seven
original intake tests retain their assertions. The bootstrap worktree predates the intake PR,
so it also contains the prerequisite source modules and headline fixtures from that source head.
The correction patch excludes those prerequisite changes.

## Verified locally

- `exact-source-red.log`: the two reported production assertions fail (`eligible=1`), while
  `agents:auto-pilot-pause` remains excluded. The production modules were read directly from the
  referenced Git commit into a separate temporary directory.
- `exact-source-green.log`: after applying the correction patch to that source copy, all 18 new
  standard-library regression tests pass. `git apply --check` also passed against the source copy.
- The new tests cover both pause labels, concrete and routing/recovery/invite/future `agent:*`
  labels, case/whitespace normalization, eligible unowned controls, scheduler rejection, launch
  ownership revalidation, and the shadow/unarmed launch gates.
- Feed tests reject array/scalar JSON, malformed items, invalid/nonfinite/future timestamps, and
  the 200-PR discovery limit. A truncated repository contributes nothing and is named as failed;
  a complete repository still contributes eligible issues and excludes issues with an open PR.
- All four affected module selftests passed; their logs are retained here.
- `git diff --check` passed. No verification floor, skip ceiling, mypy exemption, workflow,
  research launch configuration, installed mirror, or deployment setting was changed.

Tests use memory databases or temporary handoff/state paths. No live Brain or deployment was used.
`manifest.json` records the original revision and SHA-256 hashes of the source and candidate files.

## Required follow-up

The repository's `.git` directory is read-only: Git cannot create `index.lock`. Black and pytest
are missing, and installation fails because `pypi.org` cannot resolve. Both required Black
commands were attempted and failed with `black: command not found`; targeted pytest failed with
`No module named pytest`. The original intake/control/followup pytest suites have **not** passed
in this run. No Python commit is permitted until the required formatting check passes.

Automatic approval also rejected adding `needs-human` and posting a blocker comment to PR #570:
both actions require approval while this run's approval policy is `never`. Neither mutation occurred.
Both PR #566 and #570 were observed open and ready for review.

After writer reconciliation, apply the correction on the source branch, run Black with line
length 100 and the mandated repository-wide Black check, then run the original intake and
relevant control/scheduler/followup tests with `-m "not slow"`. Keep launching off. Preserve
ceilings and exemptions and measure any collection-floor update rather than estimating it.

Delivery remains pending: commit/push, complete expected checks and all check suites at the new
head, seven full minutes from that head's push/update anchor, specific disposition of the source
thread with zero active non-outdated unresolved threads, the mandatory `merge_guard` path, and
the actual `verify:compare` verdict. No delivery criterion or deployment is claimed complete.
