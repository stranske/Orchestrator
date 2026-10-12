# Issue #569: research ownership recovery

## Current keepalive validation

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
