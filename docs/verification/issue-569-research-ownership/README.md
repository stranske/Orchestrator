# Issue #569: local research ownership correction

The correction is prepared and locally tested. It is **not committed, pushed, merged, or deployed**.
The source PR #566 remains at `4a3dcf25addce6399d3267df42fb3b7063150651` with an active,
non-outdated unresolved P1 thread. Its original writer must be reconciled before branch adoption.

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
