# Issue 599: keepalive round 11 acceptance evidence

Check date: 2026-11-11 (America/Chicago). Tested implementation: `f016777027d5c141188717719dae54248cf558cd`.

Outcome: all 41 source-issue rows resolve; zero reviewed missing consumers.
The command `/opt/anaconda3/bin/python3 src/capability_admission.py` exited 0
on a fresh byte-for-byte copy of `/Users/teacher/.codex/orchestrator/capabilities.json`.
`ORCH_CAPABILITIES_PATH` selected that copy so the adjacent ledger lock was writable.
The command used the real runtime for its other inputs. This is a copied-ledger
observation, not proof that these declarations have been deployed onto the live ledger.

```text
  CONSUMERS (10th): 51 of 51 declare a consumer this tree can find — acting (capability-level) 9, in grace 0, not invoked 0, exercise-bound 21, unmeasured 0
                   not acting (trailing window) 21 (blocking 0, pre-cutoff debt 21); prose 0, missing 0, undeclared 0 — DRAINABLE 0 (reviewed absent 0, unresolved 0; reviewed absence remains finding debt)
```

The focused command was `/opt/anaconda3/bin/python3 -m pytest -q
 tests/test_capability_admission.py tests/test_capability_admission_standalone.py`
(with `PYTHONPATH=src` and disposable `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`,
and `ORCH_CAPABILITIES_PATH`). Result: 44 passed, 3 environment skips in 10.28s.
The named declaration test failed after replacing `route-weights-export`'s consumer
with prose (exit 1, identifying `KNOWN_GATES:route-weights-export`), then passed
on byte-identical source restoration (exit 0, 1.65s). No lifecycle or action credit
was changed. The 21 rows with no trailing-window action remain separately reported.

## CI failure and bounded drain

The failing `test-quality` job in Gate run `43124791570` stopped at
`Apply runtime acceptance-criteria label`: GitHub returned HTTP 403,
`Resource not accessible by integration`. It was not a failing Python assertion.
Blocking/drainable pair: label write denied / apply the required label using the
available authenticated account and rerun the failed jobs. The label was applied;
`test-quality` passed on attempt 2. No workflow permissions or pins were edited.
The general workflow label-permission defect is follow-up work in the workflow
source repository, outside this declaration repair.

## Declaration inventory

These are output-consumer bindings, not a claim of measured invocation. The older
`issue-599-consumer-bindings.md` records historical snapshots, including the old
missing docs-drift finding. The current declaration names its explicit apply-mode
handover, `scripts/docs_drift_fix_agent.py:apply_issues`; it files the proposed issues.
No current source-issue row is marked `consumer_missing: true`.

| Source issue row | Current downstream consumer |
| --- | --- |
| `abcd-experiment` | `synthesis_promotion.py:ensure_evaluated_state; objective_anchor.py:anchor_experiment` |
| `adversarial-review` | `adversarial.py:file_followup consumes the panel verdict after merge` |
| `agy-runtime-isolation` | `dispatcher.py:plan_dispatch; dispatcher.py:offload use the confined Gemini command` |
| `capability-activation-audit` | `verify.py:run_gates fails verification when the activation audit exits nonzero` |
| `capability-admission-gate` | `verify.py:run_gates fails the verify run on a blocking row; capability_admission.py:format_report prints it` |
| `capability-firing-monitor` | `switch_review.py:firing_regressions consumes persisted findings` |
| `capability-propensity` | `capability_advisor.py:advise ranks the offered candidates` |
| `codemod-campaign` | `codemod_lane.py:build_delegation_prompt consumes the campaign and dry-run plan to produce the worker prompt` |
| `coverage-testgen-trigger` | `testgen_lane.py:ranked_sources; testgen_gate.py:main` |
| `cross-repo-coordination` | `cross_repo_lane.py:build_dry_run_plan consumes the coordination proposal to produce work items and dispatch prompts for manual release` |
| `deliberate-break-verifier` | `runtime_ac.py:evaluate_results consumes deliberate-break JSON; local_verify.py:record_verdict records the run verdict` |
| `docs-drift-fix-agent` | `scripts/docs_drift_fix_agent.py:apply_issues hands repair batches to GitHub as issues on Maint 87's dispatch-only apply path` |
| `epic-decomposition` | `epic_lane.py:build_dispatch_prompts consumes the validated DAG to produce worker prompts` |
| `evidence-acquisition` | `evidence_acquisition.py:render_tick_line reads the emitted acquisition plan` |
| `feature-reflection-cli` | `periodic_report.py:format_human consumes the feature registry summary` |
| `feature-scan` | `feature_scan.py:apply_scan writes the scan's unlogged structures to the registry on --apply` |
| `feedback-store` | `router.py:learned_ranks; periodic_report.py:build_report` |
| `frontend-verifier` | `runtime_ac.py:_completed_result consumes frontend verifier exit status and JSON; runtime_ac.py:evaluate_results fails the runtime gate on failed checks` |
| `live-keepalive-supervisor` | `keepalive_supervisor.py:close_verified_issues; redirect_shadow.py:record_redirect; periodic_report.py:format_human` |
| `local-model-profile-trial` | `periodic_report.py:build_report; observability_dashboard.py:build_dashboard` |
| `offload` | `roles.py:run_prompt_agent; roles.py:run_adjudicator_agent consume backend output; feedback.py:record_role_run binds the consuming role to its backend run` |
| `partitioned-review` | `partitioned_review.py:synthesize_results validates and combines partition outputs` |
| `rail-exercise-cadence` | `periodic_report.py:_rail_exercise_summary reads the cadence artifact` |
| `range-lane-rollout` | `range_lane_rollout.py:main delegates only on the triple-guarded apply branch` |
| `redirect-plan` | `redirect_apply.py:apply_candidates consumes the recovery plan` |
| `redirect-policy` | `redirect_plan.py:plan consumes the policy decision` |
| `repo-playbook` | `dispatcher.py:build_prompt; capability_advisor.py:advise` |
| `research-scheduler` | `tick.py:research_tick launches the planned experiments` |
| `research-usage-guard` | `exp_abcd.py:followup consumes the opportunity decision before a follow-up` |
| `role-adjudicator` | `switch_review.py:format_report consumes agreement and cost summaries` |
| `role-decomposer` | `epic_lane.py:build_dispatch_prompts validates the role DAG before worker prompts` |
| `role-triage` | `triage_shadow.py:consume orders candidates only behind ORCH_TRIAGE_CONSUME; switch_review.py:format_report reports consumption` |
| `route-weights-export` | `route_weights_export.py:publish_document hands the document to the Workflows config repository` |
| `runtime-ac-checks` | `merge_guard.py:evaluate_merge_gate consumes the runtime gate verdict` |
| `stall-watcher` | `redirect_policy.py:decide consumes the classified stall` |
| `strategy-experiments` | `exp_abcd.py:prepare_arms launches the strategy's planned arms` |
| `switch-review` | `switch_review.py:raise_questions; switch_review.py:format_report` |
| `synthesis-promotion` | `synthesis_promotion.py:default_delivery_discovery links an external delivery; synthesis_promotion.py:default_mirror_outcome mirrors its measured outcome` |
| `testgen-lane` | `testgen_gate.py:main consumes candidate tests for the acceptance gate` |
| `thompson-hybrid-routing` | `router.py:select_agent dispatches the sampled challenger` |
| `windowed-capacity-policy` | `router.py:select_agent; range_lane_rollout.py:build_rollout; rate_incidents.py:record_lane_round` |

## Round 12 live-ledger acceptance

On 2026-11-11, the reported runtime virtual environment was absent. The shared
interpreter ran `/opt/anaconda3/bin/python3 -m pytest -p no:cov -o addopts= -p no:cov tests/test_capability_admission.py --tb=short`
against a byte-for-byte live-ledger snapshot selected by `ORCH_CAPABILITIES_PATH`.
On `0dfbe841` it returned **1 failed, 29 passed** in 71.21 seconds. The only
failure was `test_new_capabilities_carry_all_required_parts`. Its blockers were
`opener-core: heartbeat` and `sync-core: heartbeat`. On `main` at `2c945fa9`,
the same test and snapshot produced both heartbeat failures and the
`local-loop` caller and heartbeat failure already addressed on this branch.

The raw snapshot held zero invocation events for either core. Their deployed
entrypoints write round journals and have no capability heartbeat call. The
admission check requires a recent runtime invocation event for each external
caller. The two blockers can drain when the lane owners record a genuine
invocation from each deployed core and the same test passes on a fresh snapshot.

The named declaration test failed when the `route-weights-export` declaration
was changed to prose in a disposable copy. It passed after byte-identical
restoration. The admission CLI exited 0 and printed:

```text
  CONSUMERS (10th): 51 of 51 declare a consumer this tree can find — acting (capability-level) 9, in grace 0, not invoked 0, exercise-bound 21, unmeasured 0
                   not acting (trailing window) 21 (blocking 0, pre-cutoff debt 21); prose 0, missing 0, undeclared 0 — DRAINABLE 0 (reviewed absent 0, unresolved 0; reviewed absence remains finding debt)
```

The CLI reconciles declarations in memory. Five persisted declarations in the
snapshot still differ from this branch: `agy-runtime-isolation`,
`codemod-campaign`, `cross-repo-coordination`, `docs-drift-fix-agent`, and
`epic-decomposition`. Post-sync acceptance remains pending.

## 2026-11-12 runner follow-up

The prepared source change adds a `CONSUMER SYNC` diagnostic alongside the
unchanged CONSUMERS counters. It compares persisted and reconciled consumer
fields from one ledger read, lists pending edits on live rows, and reports an
empty ledger as unverified. It grants no admission, lifecycle, or action credit.

Seven focused checks passed by direct Python invocation, including the named
declaration check and the extended sync and absence regressions. The regressions
cover persisted sync, idempotence, stale absence flags, retired rows, an empty
runner, and one read per report. Deliberately presenting the reconciled snapshot
as persisted state failed the sync regression; restoring separate views passed.
Syntax parsing and `git diff --check` passed. These are not pytest results.

The live ledger is absent on this runner. Black and pytest are also unavailable;
installation attempts failed, and the mandatory formatting commands and focused
pytest command could not execute. The change remains uncommitted under the
pre-commit formatting gate. Follow-up must run Black with line length 100, the
required repository-wide Black check, and focused pytest with `-m "not slow"`
before committing on this branch. Actual post-sync live-ledger acceptance remains
unchecked. PR #1354 was verified open and ready for review. The connector rejected
the blocker comment and `needs-human` label because both require approval while
this run's approval policy is `never`; no hosted progress metadata was changed.

## Continuation acceptance contract — 2026-11-11

Real-input outcome command: `/opt/anaconda3/bin/python3 src/capability_admission.py`
against the actual live ledger after its existing owner's normal deployment/sync.
Success requires nonempty persisted CONSUMER SYNC, prose 0, missing 0, and
DRAINABLE equal to consumer_missing rows; trailing-window action debt stays separate.
No snapshot reconciliation or synthetic heartbeat can satisfy this outcome.
Stop rule: at most two repair attempts per named failure and 60 minutes for this
continuation; each local check is bounded below two minutes. Stop at the first
undrained failure when either limit trips. Deployment blocking/drainable pair:
owner sync not observed / existing owner deploys and syncs, then this seat reads
real state. No sync writer takeover. Initial head: 7881c0e2c3a5e0a30a3973e7328256f58f6eaf4c.
`git worktree list --porcelain` and the worktree directory inventory show only
this holder for PR 1354; it was clean. No preserved holder was found or modified.

### Continuation focused results

Tested code head: `c5cd6364` (full SHA recorded by `git rev-parse HEAD` in the
continuation log); the following documentation commit changes no tested code.
Interpreter: `/opt/anaconda3/bin/python3` (no repository `.venv` exists).
Both partitions used `PYTHONPATH=src`, separate `mktemp` directories for
`ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`, and `ORCH_CAPABILITIES_PATH`, and
`ORCH_CAPABILITY_HEARTBEATS=0`. Common pytest arguments:
`-p no:cacheprovider -o addopts= -q -m 'not slow'`.

- `tests/test_ledger_read_cache.py tests/test_capability_admission.py tests/test_capability_admission_standalone.py`: **53 passed, 3 skipped in 24.65s**.
- `tests/test_coverage_baseline_wiring.py tests/test_coverage_round_1138.py`: **92 passed in 6.68s**.
- Both report-read tests instrument `capabilities.load`, forward `create`, and
  assert exactly `[(path, False)]`, with injected context and built context.
- The real Node child running `while (true) {}` raises `subprocess.TimeoutExpired`
  at the test's 0.2-second deadline; the helper default remains 30 seconds.
- Statement regressions cover unique ordered matches, absent text, and ambiguous
  duplicate eligible text; the existing published-source checks also pass.
- Named prose break: replacing only the in-process `route-weights-export`
  consumer with `the export publisher` failed
  `test_declared_consumers_resolve_in_the_tree` with
  `KNOWN_GATES:route-weights-export`; restoring the original declaration passed.
  No source or ledger mutation was retained.
- Black `--line-length 100` on the three changed tests: one reformatted, two
  unchanged. Required repository-wide Black check: **699 files unchanged**.
  Ruff on those tests: **All checks passed!** `git diff --check`: no output.

Live command attempt on 2026-11-11 stopped with `PermissionError: [Errno 1]
Operation not permitted: '/Users/teacher/.codex/orchestrator/capabilities.json.lock'`.
It printed no CONSUMERS or CONSUMER SYNC counters. This is not post-sync evidence.
No writable-copy or reconciled-snapshot substitution was used for acceptance;
no heartbeat, deployment, or sync writer was invoked. The existing owner must
perform normal deployment/sync, then rerun the command in a context permitted to
read the actual ledger lock. Persisted consumer sync and action debt remain
unverified until that real report exists.

Current-head CI and post-push review are recorded against the final pushed SHA
in PR #1354; no required-check names were exposed by `gh pr checks --required`
at preflight (it printed `no required checks reported`). All CI checks will still
be inspected, rather than treating that absence as success.

## 2026-11-12 consumer-sync exit check (uncommitted)

The prepared code adds `python3 src/capability_admission.py --check-consumer-sync`.
It prints the existing report and exits 1 unless live rows exist, persisted
consumer declarations match this tree, and declaration DRAINABLE is zero.
Reviewed absences still count as missing and drainable debt. Trailing-window
action debt remains separate and does not fail this check. `--json` retains the
same exit behavior; other execution modes cannot bypass the check by combination.
The default report remains informational.

The existing persisted-sync regression now exercises the CLI against disposable
ledgers: absent, stale, synced with action debt, prose, unresolved, undeclared,
reviewed absent, and retired-only. It checks JSON output and unchanged ledger
bytes. Six focused regression functions passed by direct Python invocation,
including this extended regression and the named declaration check. A deliberate
prose mutation failed the named check; restoration passed. These are not pytest
results. Syntax parsing and `git diff --check` passed.

An actual CLI invocation against an explicitly empty disposable runtime exited 1
and printed `CONSUMER SYNC: unverified (no live ledger rows on this machine)`.
That is evidence for rejecting an empty runner, not live-ledger acceptance.
The default live command failed because this runner cannot access its runtime
directory. The owner's post-sync acceptance remains unchecked.

Both required Black commands and focused pytest could not run: executables and
modules are absent. Installing Black/pytest into `/tmp` failed because PyPI DNS
could not resolve; the local package cache does not contain them. The Python
changes remain uncommitted under the mandatory pre-commit Black gate. Follow-up
must run formatting and focused pytest before committing, then run the new check
against the actual live ledger after the owner's normal deployment/sync.

PR #1354 was verified open with `draft=false`. Publishing the blocker comment
and adding `needs-human` were rejected by the GitHub connector because the
operations require approval while this run's approval policy is `never`.
No hosted task metadata was changed.

## 2026-11-12 CLI process regression

Added `tests/test_capability_consumer_sync_cli.js`, following the existing Node
tests that exercise Python CLIs. Each command uses a fresh Python process with
explicit disposable runtime and ledger paths; no admission context is mocked.
The test proves that an empty ledger and a clean but unpersisted preview fail,
then invokes the production writing loader and checks successful JSON and text
reports. Persisted prose, unresolved, undeclared, and reviewed-absent consumers
still fail even with no pending declaration edits. Report commands preserve
ledger bytes, and reconciliation preserves the existing lifecycle and evidence.

`node --test --test-isolation=none tests/test_capability_consumer_sync_cli.js`
passed all five tests. The existing declaration and persisted-sync regressions
also passed by direct Python invocation; the existing route-weights CLI test
file passed under Node. Pytest and Black remain unavailable, and the attempted
installation failed because PyPI DNS could not resolve. This change edits no
Python files.

These disposable-ledger checks do not establish the owner's post-sync live
result. That acceptance checkbox remains unchecked. Run
`python3 src/capability_admission.py --check-consumer-sync` on the actual runtime
after its normal deployment/sync and retain the CONSUMERS and CONSUMER SYNC
output. Adding `needs-human` on PR #1354 was denied by the GitHub connector:
the operation requires approval, while this run's approval policy is `never`.
Posting the blocker comment was denied for the same reason. PR #1354 was
verified open with `draft=false`.

The commit attempt failed before staging: Git could not create `.git/index.lock`
because `.git` is mounted read-only. The new test file and this evidence update
remain uncommitted and must be committed from a checkout with writable Git
metadata. Syntax and whitespace checks passed.

## 2026-11-12 mixed-lifecycle CLI regression

Reviewed recent commits through `51438f31` and reconciled them with PR #1354's
body: all five verified tasks are already checked. The remaining post-sync
live-ledger acceptance criterion stays unchecked. This runner has no default
live ledger and no explicit `ORCH_CAPABILITIES_PATH` configuration.

Extended `tests/test_capability_consumer_sync_cli.js` to exercise both retired
and superseded rows in fresh CLI processes. A terminal-only ledger must fail
as unverified. Adding a live row with an unpersisted consumer must still fail,
even when the reconciled CONSUMERS preview is clean. Persisting only the live
binding must pass without requiring edits to terminal rows or granting action
credit. Verification preserves the ledger bytes and terminal lifecycle/evidence.

`node --test --test-isolation=none tests/test_capability_consumer_sync_cli.js`
passed **7 tests**. Both new tests also passed in a disposable source copy and
failed after deliberately changing the live-population filter to include
terminal rows. The original checkout was not mutated. Seven existing Python
regressions passed by direct invocation, including the named declaration test
and persisted-sync regression. A deliberate prose mutation failed the named
test; restoring the declaration passed. The focused pytest command with
`-m 'not slow'` could not run because pytest is absent. No Python files changed.
Node syntax validation and `git diff --check` passed.

The GitHub connector rejected the PR-body reconciliation update because it
requires approval while this run's approval policy is `never`. No hosted
checkboxes were changed. The existing owner still needs to perform its normal
deployment/sync and run `python3 src/capability_admission.py --check-consumer-sync`
against the actual live ledger, retaining both CONSUMERS and CONSUMER SYNC output.

The commit attempt stopped at staging: `git add` could not create `.git/index.lock`
because Git metadata is mounted read-only. The tested JavaScript changes and this
evidence remain uncommitted and require a checkout with writable Git metadata.
PR #1354 remains open and ready for review (`draft=false`).

## 2026-11-12 measured-action CLI regression

Reviewed commits `51438f31` and `b0379795` against the PR body. The five checked
tasks already reflect verified implementation; the owner's post-sync live-ledger
criterion remains unchecked. This runner has no default ledger at
`/home/runner/.codex/orchestrator/capabilities.json` and no explicit
`ORCH_CAPABILITIES_PATH`.

Added a fresh-process CLI regression for the remaining distinction in that
criterion: measured action debt is separate from declaration completeness.
The test starts with a persisted, resolvable offload declaration and no Brain,
then initializes a disposable SQLite Brain with the production schema. The
report changes from `unmeasured` to `not_acting`, with no acting credit and
`drained=false`, while `--check-consumer-sync` still exits zero in both JSON and
text modes. Both modes preserve the ledger bytes and report declaration
`DRAINABLE 0`. No context or monitor result is mocked.

`node --test --test-isolation=none tests/test_capability_consumer_sync_cli.js`
passed **8 tests**, with no skips. Node syntax validation and `git diff --check`
passed. No Python files changed. These are disposable-ledger results, not
evidence of an owner sync.

The commit attempt failed at `git add` because `.git/index.lock` cannot be
created on the read-only Git metadata mount. The test and this note remain
uncommitted. The GitHub connector rejected the PR-body update, blocker comment,
and `needs-human` label because each requires approval and this run's approval
policy is `never`. No hosted task metadata changed. The owner still needs to
sync normally and retain the actual runtime's CONSUMERS and CONSUMER SYNC output
from `python3 src/capability_admission.py --check-consumer-sync`.
# 2026-11-12 full-inventory runner follow-up

The prepared JavaScript CLI regression starts all 52 current declarations as
legacy prose, runs the production writing loader, and verifies persisted sync,
the zero prose/missing/DRAINABLE counters, and preserved lifecycle state and
prior evidence. An additional ledger-only unresolved consumer then fails the
check with DRAINABLE 1 even though no declarations await sync.

`node --test --test-isolation=none tests/test_capability_consumer_sync_cli.js`
passed all 9 tests without skips. Node syntax and `git diff --check` passed.
A disposable test mutation replacing the writing loader with its read-only
preview failed as expected. No Python files changed.

The commit failed because `.git/index.lock` cannot be created on the read-only
Git metadata mount. The test remains uncommitted. PR comment and `needs-human`
label writes were rejected because they require approval and this run's approval
policy is `never`. PR #1354 was verified open and ready for review. The actual
live ledger is unavailable here; post-sync acceptance remains unchecked.

## 2026-11-12 post-sync declaration drift regression

Reviewed recent commits through `09ae0356` against PR #1354's current body.
The five verified tasks are already checked. The remaining owner's post-sync
live-ledger criterion stays unchecked: the configured live ledger is absent on
this runner.

Added a CLI regression that first persists a successful sync, then changes the
offload binding to another resolvable symbol. Clean CONSUMERS counters must not
mask that persisted drift: JSON and text verification fail with a pending
`downstream_consumer` edit and preserve the ledger bytes. The normal writing
loader repairs the binding, verification succeeds again, and lifecycle state
and prior evidence remain intact.

All ten CLI tests passed with no skips; syntax and whitespace checks passed.
A disposable test copy that bypassed the deployment check failed at the drift
verification with actual exit 0 versus expected exit 1. Three Python checks passed by
direct invocation: declared consumers, persisted declaration sync, and stale
absence flag reconciliation. The focused pytest command with `-m 'not slow'`
could not run because pytest is unavailable. No Python files changed.

Git staging failed because `.git/index.lock` cannot be created on the read-only
Git metadata mount; these changes remain uncommitted. The GitHub approval gate
rejected the PR-body reconciliation update, blocker comment, and `needs-human`
label because each requires approval while this run's policy is `never`.
PR #1354 remains open and ready for review (`draft=false`).
The existing owner must sync normally and retain the actual runtime output of
`python3 src/capability_admission.py --check-consumer-sync`; fixture results do
not satisfy the remaining acceptance criterion.

## 2026-11-12 partial owner-sync regression

Reviewed recent commits through `191d882c` and reconciled PR #1354's checkboxes:
the five verified tasks are already checked; post-sync live-ledger acceptance
remains unchecked. The default CLI cannot open this runner's configured ledger
because `/home/runner/.codex/orchestrator` is on a read-only filesystem.

Added a fresh-process CLI regression with two live rows and a partial sync.
It verifies pending fields independently, clears one stale absence flag, and
checks that the other row still fails verification despite zero prose, missing,
and DRAINABLE in the reconciled preview. Persisting the final binding passes
without changing lifecycle, prior evidence, or action credit. JSON and text
verification preserve ledger bytes.

`node --test --test-isolation=none tests/test_capability_consumer_sync_cli.js`
passed **11 tests**, with no skips, in 97.16 seconds. In a disposable source
and test copy, the new test passed before mutation and failed at the partial-sync
exit assertion after changing the guard to reject only when every row is pending.
The checkout was unchanged by this mutation. The named declaration test also
passed by direct Python invocation, rejected a deliberate in-process prose
mutation, and passed after restoration. Node syntax and `git diff --check`
passed. The focused pytest command with `-m 'not slow'` could not run because
pytest is unavailable. No Python files changed.

The connector rejected the PR-body reconciliation update, blocker comment,
and `needs-human` label because each requires approval while this run's approval
policy is `never`. Hosted task metadata is unchanged. The owner still needs to
perform normal deployment/sync and retain the actual live runtime's CONSUMERS
and CONSUMER SYNC output from
`python3 src/capability_admission.py --check-consumer-sync`.

The commit attempt stopped at `git add`: `.git/index.lock` cannot be created
because Git metadata is mounted read-only. The tested JavaScript regression
and this evidence note remain uncommitted and require writable Git metadata.
