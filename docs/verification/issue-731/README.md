# Issue #731 evidence

## Baseline inspection

`skipped-issues.json` records inspection via `gh issue view` of each of the first
28 targets in the persisted producer `skip_issues` list. A later 29th target is
outside the issue's original baseline. Each row includes its issue URL, body hash,
classification, and whether it has a nonempty Acceptance Criteria section.

| Old admission failure class | Issues |
| --- | ---: |
| Concrete test file / unsupported JS runner | 3 |
| CI tests mentioned without a named test | 24 |
| No named test or acceptance section | 1 |

The file class comprises Workflows #3970, Workflows #3967, and trip-planner #1905.
More precisely, two lack a recognized command targeting a concrete file; #3967
already specifies `node --test`, which the old recognizer rejected. #3970 has an
npm shorthand selector plus the concrete JS file elsewhere. These are reported as
`file-only` in producer metrics, meaning a concrete file was available to admission.
All 24 CI-only issues have acceptance criteria; Orchestrator #720 has neither.
This classifies issue bodies, not 28 distinct agent/task coverage cells.

## Verification

- `producer-green.log`: `python3 -m pytest tests -q -k producer`.
- `producer-selftest.log`: `python3 src/experiment_producer.py --selftest`.
- `switch-review-selftest.log`: `python3 src/switch_review.py --selftest`.
- `file-admission-red.log`: in an isolated source copy, disable the path fallback
  in `named_test()` by replacing `paths = named_test_paths(spec)` with `paths = []`.
  The admission test fails because a file-only body no longer yields
  `pytest tests/test_x.py`.
- `file-admission-restored-green.log`: restore the exact source bytes and rerun
  that test successfully. The delivery worktree was never left broken.

The existing admission test now also exercises real local Git commits and pytest
assertion failure: the first selected agent's test-spec worker commits only a test,
the harness freezes its command/commit/hash, and both comparison spawns observe
that identical seed. Passing tests, collection errors, implementation changes,
and dirty seeds stop before any comparison spawn. Altering the frozen test in an
arm disqualifies it from winner verification. Agent CLIs are stubbed in this proof;
no production experiment or live provider-quality claim is made.

Test collection remains unchanged: the existing admission/report/skip tests were
extended, and the Git derivation proof is an ordinary helper called by the
admission test. `.verify-floor.json` is byte-identical to `origin/main` and
`src/provision.py` is unchanged. Python for local verification is
`/opt/anaconda3/bin/python3`.

## Runtime boundaries and remaining observation

The derive step is synchronous and bounded: up to 600 seconds for the first
selected agent to write the test (its process group is stopped on timeout), then
up to 120 seconds for the failing-test proof. It uses the existing runtime/auth
wrapper, commit-enabled agent sandbox, and no-push Git environment. Derived tests
currently use one Python test file; explicit Python and JS tests use their named
commands, with nested npm/pnpm packages and native `node:test` recognized.
The cadence distribution and four-experiment daily cap are unchanged.

Only launched experiments with `test_source=derived` increment `derived_tests`.
Missing tests and failed freezing are counted once per issue/UTC day, with the
file-only / ci-only / none class. Historical skips without stored classes are
printed as UNKNOWN rather than assigned fabricated classes.

The October 25 requirement (no_named_test at most 25% of considered cells) remains
UNMEASURED. Admission of a body is not evidence that its future derived test or
comparison will succeed. A separate ready PR #734 also targets #731; this branch
does not change that PR or its branch.

Historical skips are cumulative, while `cells_total` counts agent/task coverage
cells. A current-window percentage requires a comparable observation denominator;
dividing the retained historical skip count by `cells_total` does not prove the
October 25 target.

## Full-verifier limitations

The first complete `python3 src/verify.py` returned nonzero: 4951 passed,
37 failed, 4988 collected; four selftests failed. Its capability gates passed,
but entrypoint enumeration was NOT CHECKED because the shared
`capabilities.json.lock` path was denied.
`verify-initial.log` preserves the complete verifier summary. This is not a green
full-suite result and is not claimed to fail only on ps/network/Seatbelt.

Rerunning those 37 failed nodes with TMPDIR=/private/tmp and a private
ORCH_AGENT_RUNTIME_DIR made 13 pass; 24 still failed
(`verify-failure-isolation.log`). Those 24 comprise:

- 10 tick-watchdog cases: process inspection (`ps`) is denied, including its
  timeout/arming consequences. Direct `pgrep -a` also cannot get the process list.
- 4 Seatbelt quoting cases: `sandbox-exec: sandbox_apply: Operation not permitted`.
- 4 documented-copier cases: a child `mktemp` still selects the denied system
  `/var/folders/.../T` directory.
- 3 profile-trial cases and 1 offload case: writes to shared state/handoff are
  denied; all four pass when ORCH_STATE_DIR and HANDOFF_DIR point to temporary
  directories (`profile-isolation-regression.log`).
- 2 model-tier cases: they inherit an `auth-failed` availability verdict rather
  than the expected mocked model-health outcome. The underlying cause of that
  host auth verdict is UNKNOWN; no credentials or auth gates were changed.

The two auth-dependent failures and one copier failure reproduce against an
untouched `git archive origin/main` at deeee36f650814e180d3a83e799763ea4e332266
(`main-baseline-diagnostic.log`). That archive has no .git; pytest explicitly
reports that its tracked-input check is not performed. The selected test cases
still execute and reach the same failures. Relevant capacity, auth, copier,
adapter, propensity and watchdog source is unchanged in this PR.

Additional supported-environment rechecks:

- 43 workspace/private-state isolation tests pass with TMPDIR=/private/tmp and a
  private agent runtime (`isolation-regression.log`).
- 24 experiment arm/subject regressions pass with a private agent runtime
  (`arm-regression.log`).
- adapters selftest passes with TMPDIR=/private/tmp
  (`adapters-env-selftest.log`).
- propensity selftest passes with TMPDIR=/private/tmp and private
  ORCH_LOCAL_RUNTIME / ORCH_STATE_DIR (`propensity-env-selftest.log`). Its initial
  tripwire was seeing temporary stores and this source worktree under the live
  control-plane root.

No OS privacy/security settings, permission gates, shared runtime configuration,
or provisioning logic were changed to obtain these results.

The second full run, with a private ORCH_AGENT_RUNTIME_DIR, is also NOT GREEN:
4960 passed / 28 failed / 4988 collected; four selftests failed (adapters,
capability_propensity, dispatcher, tick_watchdog). See `verify-private-runtime.log`.
Runtime-isolation assertions also depend on temporary directory placement and
canonical runtime path shapes. The targeted environment rechecks above establish
which subsets pass; neither full run is presented as a full-suite PASS.
Both full runs precede the final upstream refresh from main.
