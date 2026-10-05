# Issue 417: batch offering and per-decision cap proof

The existing `run_prompt_batch` API routes once and records each body independently.
This change extends that API and the existing advisor/decline event path; it creates
no new capability, event log, or dispatch path. The concept search, current capability
inventory, and improvement-log search confirmed existing PromptAgent and batching.

The advisor binds `role-prompt` at `research-program` (driver consult) and
`repo-audit:phase-4` (issue filing). Single-body / one-prompt / no-batch reasons
are classified as non-demotable `wrong_moment` on new writes and on historical reads;
the detector regression checks the original ledger bytes remain unchanged.
The automatic dispatch gate remains off when `ORCH_ROLE_SHADOW` is unset;
the delegate CLI prints that fact once. Explicit batch authoring does not flip it.

## Deliberate-break and restoration

Each control used a temporary copy of one module ahead of `src` on pytest's
configured import path. Only the private copy was mutated and restored.
Every control returned exit 1 with one named failure, then exit 0 with one named
pass after restoration; the checkout source remained byte-identical throughout.

| Mutation | Named test in `tests/test_role_prompt_offering.py` | Broken | Restored |
| --- | --- | --- | --- |
| surface | `test_research_program_surface_is_declared_and_binds_role_prompt` | 1 failed, exit 1 | 1 passed, exit 0 |
| demotion | `test_wrong_moment_declines_never_demote` | 1 failed, exit 1 | 1 passed, exit 0 |
| per-body-cap | `test_a_batch_counts_once_against_the_cycle_cap` | 1 failed, exit 1 | 1 passed, exit 0 |

The cap control increments the prompt-role counter for each body. A three-body
batch then reports more than one invocation at the next capped decision, which
the regression rejects. A normal batch reports one invocation and keeps three
distinct role runs; a second batch is withheld without publishing bodies.

Commands: `python3 -m pytest tests/test_role_prompt_offering.py -q`,
`python3 src/verify.py`, and the named test selections with
`-o "pythonpath=<private-module-dir> <checkout>/src <checkout>/tests"` for each control.
Complete RED/GREEN logs and JSON receipts are retained in the automation's
`evidence/20261005T0202Z` directory. CI's full verifier remains authoritative for
the pushed PR head. No live mirror deployment is part of this PR.

Initial checkout verdict: `python3 src/verify.py` passed 1,747/1,747 collected tests,
99/99 selftests, all five gates, and all 115 mypy modules with no exemptions.
The six focused cases also passed; the measured floor is 1,747, with no skip or
exemption ceiling increase.

After concurrent PR #452 merged, this branch was rebased onto main `1f06bc3`.
Both floor histories were preserved; measured collection is 1,757. The rebased
full verifier passed 1,757 tests, 99 selftests, all five gates, and all 115 mypy
modules. The six new cases account for this branch’s collection delta; all ceilings
remain unchanged.

## Keepalive follow-up on 2026-10-05

Extended `test_a_batch_counts_once_against_the_cycle_cap` to cover an explicit
`ORCH_ROLE_MAX_PER_CYCLE=2`: a three-body batch and a two-body batch consume two
decisions, produce five distinct role runs, and withhold a third batch without
exporting bodies. The existing test count is unchanged.

The configured-cap scenario passed a separate standard-library behavioral replay
using temporary Brain state and mocked backend calls. The roles and propensity
selftests passed; the advisor selftest passed with a temporary ledger, reporting
its machine-local front-door prerequisite as absent. Syntax validation and
`git diff --check` passed.

That keepalive runner lacked pytest and Black and could not install them. Its
standard-library replay was an interim check. The follow-up is now committed;
the focused pytest and full Black results below supersede that tool limitation.

## Decline wording follow-up on 2026-10-05

Extended the existing named wrong-moment regression without adding collected cases.
It now checks ten hyphenated, spaced, and mixed-case reasons on historical reads,
and all ten on new writes with `unspecified`, `scope_too_small`, and explicit
`wrong_moment` kinds. The detector must report all 60 events as non-demotable
wrong-moment declines and leave the ledger bytes unchanged on both reads.

Standard-library replay copied the three named test functions into a temporary
runner and supplied temporary paths and `unittest.mock` patches. Each function
passed against the current modules, failed with `AssertionError` against its
required deliberate-break private module copy, and passed after restoring that
copy. SHA-256 checks confirmed all three checkout source modules were unchanged.
The replay script and output are `/tmp/issue-417-replay.py` and
`/tmp/issue-417-replay.log` in this runner; these temporary artifacts are not
durable CI evidence.

The replay was an interim check from a runner without pytest or Black. The
follow-up is now committed and its named regressions execute under pytest in
the opener recovery below; current-head CI remains the full acceptance gate.

## Recorded-consult follow-up on 2026-10-05

Extended the first named regression to exercise recorded consults at both batch
authoring surfaces, for classified tasks and classification misses. It checks that
`role-prompt` enters the experiment candidate set with the correct surface
attribution, repeated consults add no duplicate matches or events, and
`record=False` leaves the ledger byte-identical. The collected case count is unchanged.

The standard-library replay at `/tmp/issue-417-replay.py` passed all three named
functions, rejected each required deliberate break in a private module copy with
`AssertionError`, and passed after each restoration. Source hashes confirmed the
three checkout modules were unchanged. Output is `/tmp/issue-417-replay.log`;
these runner-local artifacts are not durable CI evidence. Syntax parsing and
`git diff --check` also passed.

The earlier runner could not run pytest or Black or reach the GitHub API. That
checkpoint is superseded by the committed follow-up and the verified recovery
below; it does not describe the current checkout or PR readiness.

## Opener recovery on 2026-10-05

Current-head Gate run `37267192621` failed only Black formatting in
`tests/test_role_prompt_offering.py`. The exact full formatter command was
reproduced red, then passed after formatting that file. Its parsed AST stayed
identical. The focused named pytest suite and `git diff --check` passed.
Architecture prose and the SVG now agree on explicit batch-authoring bindings
while automatic dispatch remains shadow-gated. Full current-head CI must run
again after this recovery push; historical full-verifier receipts above remain
identified by the checkout on which they were measured.

## Closer integration and acceptance controls

The closer retained opener recovery `d347a50`, including the diagram correction,
and integrated merged reader repair #465 at main `1c80a69`. Current collection is
1,861 with every skip and exemption ceiling retained. Before integration, 28 focused
batch/offering cases, propensity and dispatcher selftests, Black/Ruff and focused
mypy passed. Current named controls independently observed: removing the consulted
research surface, making wrong_moment demotable, and counting each body against the
cap each fail their named test; exact private-module restoration passes. An additional
classification-removal control also fails and restores. Current-head CI and the
populated-state scratch-mirror verdict remain explicit separate gates; no live
publication is claimed. Receipts: closer `work/20261005T0520Z`.

## Reproducible control runner follow-up

`python3 scripts/verify_role_prompt_offering.py` now reproduces all three required
mutations using private module copies. It requires a baseline pass, exactly one
named assertion failure under the mutation, and a restoration pass. Pytest's JUnit
report confirms the test actually ran; missing reports, usage/import errors, skips,
and unrelated failures cannot satisfy the control. The private copy is restored
even on failure, bytecode reuse is disabled for each child, and checkout source
bytes are checked throughout. Successful runs print JSON receipts with pytest output.

This runner lacks pytest and Black across its installed Python interpreters.
Installing the tools failed because PyPI DNS resolution is unavailable. The runner
correctly rejects its missing-pytest baseline rather than reporting acceptance.
The required Black format and repository-wide check cannot execute, so these Python
changes must remain uncommitted until that gate passes.

Interim standard-library validation directly invoked all three existing named test
functions: each passed, failed with `AssertionError` under its exact private-module
mutation, and passed after restoration. Report-validation checks also rejected
missing reports, import errors, unrelated failures, unexpected exits, and skips.
Checkout source remained byte-identical. Replay and logs are runner-local at
`/tmp/issue-417-control-replay.py` and `/tmp/issue-417-control-replay.log`; this is
not a pytest receipt or durable CI evidence. The remaining acceptance checkbox
stays unchecked pending the real pytest runner and formatting gates.

The GitHub connector also blocked adding `needs-human` and posting the blocker
comment: both mutations require approval, while this run's approval policy is
`never`. Read-only metadata confirmed PR #457 is open and ready for review.

## Fresh-receipt follow-up

The control runner now deletes any earlier report before each pytest invocation
and rejects unknown phase names. This prevents a retry from accepting an old
baseline, broken, or restored receipt when the current child writes no report.
`tests/test_role_prompt_offering_controls.py` exercises the real parser with
stale and fresh reports for all three phases, mocking only the pytest child.

`python3 -m unittest discover -s tests -p test_role_prompt_offering_controls.py -v`
passed one test. Removing report deletion in a private runner copy produced six
assertion failures within that test; restoring the copy passed. Checkout runner
bytes remained unchanged during this deliberate-break control.

This is evidence for the receipt regression, not completion of the three required
pytest controls. Every installed interpreter lacks pytest and Black; PyPI DNS
resolution failed. The real control runner rejected its missing-pytest baseline,
and both required Black commands failed because Black is unavailable. These
changes remain uncommitted under the pre-commit formatting gate. The acceptance
checkbox remains unchecked. Once pytest is available, measure collection and
update `.verify-floor.json` for the new test before running the full verifier;
no floor or ceiling is changed on the basis of the unittest run.
