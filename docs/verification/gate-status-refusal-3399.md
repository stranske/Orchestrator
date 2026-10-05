# Same-repository Gate status refusal — Workflows #3399

Source: https://github.com/stranske/Workflows/issues/3399. This bounded consumer-owned Gate repair does not close the 17-repository campaign.

Baseline `d326e59f5afeffd078cae488daa6b15a091640ac` lets the real retry helper return null for a refused commit-status write on a same-repository PR. The workflow then publishes nothing and succeeds, retaining whatever older status was already present. Forks have an expected read-only token; same-repository refusals are unexpected and must fail visibly.

The production workflow scripts run under Node with the actual retry helper; only the GitHub client and core are test doubles. New expectations over the unchanged baseline workflow fail for same-repository success/failure verdicts and the helper's 404 refusal route: **3 failed, 21 deselected**. After the null-response guard, the entire existing fork/deleted-fork, same-repository, rate-limit and origin/comment suite passes: **24 passed**. No tokens, permissions, event triggers or shared helper semantics changed. The fork summary fallback and non-success verdict floor remain intact.

Commands:

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q --override-ini addopts= -k same_repo_refusal
/opt/anaconda3/bin/python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q --override-ini addopts=
/opt/anaconda3/bin/python3 -m black --check --line-length 100 --exclude '(\.venv|\.workflows-lib|node_modules)' .
/opt/anaconda3/bin/python3 -m ruff check tests/test_gate_commit_status_fork_tolerance.py
git diff --check
```

Full Black checked 297 files. This is repo-specific recovery of Orchestrator's consumer-owned create-only Gate, as requested by the source campaign. Current exact-head CI, full review-thread enumeration, seven-minute floor, repository merge_guard and post-merge comparison remain required. No installed mirror publication or source issue closure is claimed.

Keepalive verification on 2026-10-04 strengthens the refusal controls to assert the exact head and verdict in the diagnostic, a single publication attempt, and no fork fallback. A successful same-repository publication is now exercised alongside a successful fork publication. The suite still collects 24 tests, so no collection floor changes are needed. This round leaves the production workflow untouched.

The baseline was read with `git show d326e59f5afeffd078cae488daa6b15a091640ac:.github/workflows/pr-00-gate.yml` through a temporary module-scoped fixture overriding only `_workflow_lines`; the actual retry helper and all assertions were retained. That control produced **3 failed, 21 deselected**. The current production workflow produced **24 passed**; adding the CI configuration and verdict-replay tests produced **48 passed**. Every pytest command used `-m "not slow"`.

The broad local run produced **1371 passed, 26 failed, 24 skipped**: 22 failures attempted writes under the runner's read-only `~/.codex`, two needed the unavailable coverage package, and two synthesis-promotion checks reported verifier errors. This is not a full local-suite PASS claim. The existing remote head `ccd4a71` has passing Gate/Python CI; fresh exact-head CI remains required after this follow-up. Pinned pytest 9.1.1 and Black 26.5.1 sources were obtained from their upstream GitHub repositories into `/tmp` because the runner had neither tool and could not reach PyPI.

The required repository-wide Black check, with line length 100 and exclusion `(\.workflows-lib|node_modules)`, passes for all **297 files**. `git diff --check` also passes.

Verified task status for this round:

- [x] Fail visibly on same-repository null commit-status publication while retaining expected fork handling.
- [x] Three unchanged-production-workflow refusal controls fail; the repaired 24-test suite passes.
- [x] Fork/deleted-fork and rate-limit behavior remain covered.
- [ ] Apply this follow-up to the PR branch and pass fresh exact-head CI; retain the seven-minute floor and repository merge_guard before merging.

The workspace `.git` is read-only, so the follow-up source/test commit was created in `/tmp/gate-status-refusal-review`; its patch is `/tmp/gate-status-refusal-followup.patch`. The validated two-file change also remains in the runner working tree for a writable runner to commit. GitHub checkbox, `needs-human` label, and blocker-comment updates were attempted but refused with `MCP tool call requires approval, but approval policy is never`; the remote task checkbox therefore remains unchanged. PR #413 was verified open and ready for review.

The next keepalive follow-up makes the baseline control reproducible without a temporary pytest
fixture or any production workflow edit. `GATE_TEST_WORKFLOW` selects an exported workflow;
without it the harness reads production as before. The existing extraction test verifies export
selection, identical script extraction, restoration of the default, and failure on a missing
export. Collection remains 24 tests.

```sh
git show d326e59f5afeffd078cae488daa6b15a091640ac:.github/workflows/pr-00-gate.yml > /tmp/gate-status-refusal-baseline.yml
GATE_TEST_WORKFLOW=/tmp/gate-status-refusal-baseline.yml python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q -m "not slow" --override-ini addopts= -k same_repo_refusal
python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py -q -m "not slow" --override-ini addopts=
python3 -m pytest tests/test_gate_commit_status_fork_tolerance.py tests/test_ci_gate_config.py tests/test_gate_replays_pytest_verdicts.py -q -m "not slow" --override-ini addopts=
python3 -m black --line-length 100 tests/test_gate_commit_status_fork_tolerance.py
python3 -m black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
git diff --check
```

Verified with repository-pinned pytest 9.1.1: the unchanged baseline yields **3 failed, 21
deselected** at the same-repository refusal assertion, production yields **24 passed**, and the
three-file regression run yields **48 passed**. The workflow, retry helper, permissions and event
configuration are untouched by this follow-up. The runner lacks pytest and Black and cannot
reach PyPI, so upstream sources for the pinned tools were staged in `/tmp/gate-review-tools`
and invoked with `PYTHONPATH=/tmp/gate-review-tools /usr/bin/python3` (Python 3.12.3).
Fresh exact-head CI, the seven-minute review floor and repository merge_guard remain required;
this local validation does not claim a merge or full-suite pass.

The required full Black check passes: **297 files would be left unchanged**. This sandbox
refuses socketpair writes with `EPERM`, preventing asynchronous worker results from waking
Black's event loop. An opt-in periodic wakeup in `/tmp/gate-review-tools/sitecustomize.py`
addresses that runner limitation; formatting and validation are unchanged. The successful
command used `GATE_REVIEW_POLL_WORKERS=1 BLACK_NUM_WORKERS=1
BLACK_CACHE_DIR=/tmp/gate-review-black-cache` alongside the tool `PYTHONPATH` above.
`git diff --check` passes. The source/test commit is prepared in the writable bare repository
`/tmp/gate-status-refusal-review.git`, and the applyable patch is
`/tmp/gate-status-refusal-controls.patch`; the runner working tree retains the same two-file
change because its `.git` directory cannot be written.

The next verification round covers every same-repository verdict (`success`, `failure`,
`error`, `pending`) through both the helper's 403 and 404 permission-refusal routes. Cases
are grouped under the existing three refusal controls, retaining the 24-test collection and
the unchanged-baseline result of **3 failed, 21 deselected**. Production passes **24 tests**;
the Gate/configuration/verdict-replay regression run passes **48 tests**, all with
`-m "not slow"`. Exports with the guard deliberately omitting `error` or `pending` produce
**2 failed, 1 passed, 21 deselected**, demonstrating that the added assertions detect those
regressions. Only temporary exports were mutated; production workflows and helpers are unchanged.

Reconciliation reviewed `ccd4a71`, `8114039`, and `1e7752a`; the current PR changes three
files, not the 34 claimed by the task prompt. GitHub reads confirmed PR #413 is open and ready
and that Gate, Python 3.12/3.13, lint, format, and typecheck CI pass on `1e7752a`.
The PR-body checkbox update, `needs-human` label, and reconciliation comment were all rejected
with `MCP tool call requires approval, but approval policy is never`. Remote tracking remains
unchanged. Fresh CI for this follow-up and the seven-minute review floor remain required;
any terminal merge must still use the repository merge_guard. No merge was attempted.

Black 26.5.1 formatted the changed test file and the required repository-wide check passed:
**297 files would be left unchanged**. The upstream-source tools and worker-wakeup workaround
described above were staged again in `/tmp/gate-review-tools`; `git diff --check` passes.
The workspace Git directory is read-only. The follow-up commit is therefore prepared in
`/tmp/gate-all-verdicts-review.git`, with its applyable patch at
`/tmp/gate-all-verdicts.patch`; both changed files remain in the working tree for automation
to commit on the PR branch.

The fork compatibility follow-up covers all four verdicts for both forks and deleted forks
through both permission-refusal routes (403 and 404). Assertions require the exact failure
diagnostic for non-success verdicts, one summary write, one status request against the PR head,
and permission/read-only warnings without misclassifying the refusal as a rate limit. The
existing rate-limit controls remain intact and collection remains 24 tests.

Verification: unchanged `d326e59` export **3 failed, 21 deselected**; production **24 passed**;
Gate/configuration/verdict-replay regression run **48 passed**. A temporary production export
whose fork fallback excludes deleted repositories yields **7 failed, 1 passed, 16 deselected**
with `-k 'fork_refusal or deleted_fork'`, proving the compatibility checks detect that regression.
All pytest runs use `-m "not slow"`. Black 26.5.1 formats the changed test and the required
repository-wide check passes for **297 files**; `git diff --check` passes. Production workflows
and helpers are unchanged by this follow-up.

Reconciliation reviewed all four PR commits through `5b8985e`: the PR changes three files,
and its current Gate and CI runs pass. PR #413 remains open and ready for review. Updating its
first two acceptance checkboxes and adding `needs-human` were rejected with `MCP tool call
requires approval, but approval policy is never`. The checkout's Git directory is read-only,
so the source/test commit is prepared in `/tmp/gate-fork-verdicts-review.git` and its patch at
`/tmp/gate-fork-verdicts.patch`. Applying it to the PR branch and passing fresh exact-head CI
remain outstanding; the seven-minute review floor and repository merge_guard remain required.

The current follow-up runs each same-repository refusal case against two different PR heads.
Both the publication request and refusal diagnostic must identify the selected head for every
verdict through the 403 and 404 routes. Collection stays at 24 tests. An unchanged `d326e59`
export produces **3 failed, 21 deselected**; production passes **24 tests** and the three-file
Gate/configuration/verdict-replay regression run passes **48 tests**. A temporary export with
the diagnostic hardcoded to the original head produces **3 failed, 21 deselected**, specifically
at the new-head assertions. All pytest runs use `-m "not slow"`. The production workflow and
retry helper are unchanged by this follow-up.

Remote checks on `b997e06003f9c4d01819211df8196c408b13346d` pass for Gate, Python 3.12/3.13,
lint, format, typecheck, verification, mirror verification, and combined coverage. PR #413 is
open and ready for review. These results predate this test change; applying the follow-up and
passing fresh exact-head CI remain required before the seven-minute floor and merge_guard.

Black 26.5.1 leaves the changed test unchanged; the required repository-wide check with line
length 100 and exclusion `(\.workflows-lib|node_modules)` passes for **297 files**.
`git diff --check` passes. This runner again uses upstream-source tools staged in `/tmp`
and the periodic worker wakeup described above.

The branch commit is blocked by the checkout's read-only `.git` (`index.lock` creation fails).
The source/test commit is saved in `/tmp/gate-head-review.git`, with an applyable patch at
`/tmp/gate-head-review.patch`; both changed files remain in the working tree for automation.
The first two acceptance criteria are locally verified. Fresh exact-head CI, the seven-minute
review floor, and the repository merge_guard remain outstanding; no merge is claimed.

The rate-limit follow-up exercises all three origins (same repository, fork, deleted fork),
all four verdicts, and all three rate-limit routes (403 message, 429 status, exhausted headers).
It asserts the exact non-success failure diagnostic, no permission/fork fallback, and the PR
head and verdict in every attempted status write. The suite still collects 24 tests.
Production passes **24 tests**; the Gate/configuration/verdict-replay run passes **48 tests**.
The unchanged `d326e59` export still yields **3 failed, 21 deselected**. A temporary export
that drops the rate-limit failure floor only for deleted forks yields **3 failed, 1 passed,
20 deselected**, proving the expanded checks detect that regression. All runs use
`-m "not slow"`; production workflows and shared helpers are unchanged by this follow-up.

Reconciliation reviewed the six PR commits through `7e88724`: the PR changes three files,
and the first two acceptance criteria are verified. PR #413 is open and ready for review.
The connector refused both the PR-body checkbox update and the `needs-human` label with
`MCP tool call requires approval, but approval policy is never`. The checkout's `.git` remains
read-only, so a separate commit and patch in `/tmp` are required for automation to apply.
Fresh CI on that commit, the seven-minute review floor, and repository merge_guard remain
required before merging.

Black 26.5.1 formatted the changed test; the required repository-wide check passes for
**297 files**, with the `/tmp` tools and periodic worker wakeup described above.
`git diff --check` passes. The source/test commit is prepared in
`/tmp/gate-rate-limit-review.git`, with its applyable patch at `/tmp/gate-rate-limit-review.patch`;
the two changed files remain in the working tree for automation to commit on the PR branch.

The 2026-10-05 follow-up covers empty and invalid verdict inputs, which the production step
normalizes to `pending`. Both 403 and 404 same-repository refusals must still throw with that
computed verdict and the selected PR head. The existing three refusal controls now include
these cases, keeping collection at 24 tests. The unchanged baseline yields **3 failed, 21
deselected**; production yields **24 passed**; the Gate/configuration/verdict-replay run yields
**48 passed**, all with `-m "not slow"`. A temporary export whose refusal guard only handles
recognized raw verdicts yields **2 failed, 1 passed, 21 deselected**, detecting the new regression.
Production workflows, helpers, permissions and events are unchanged in this follow-up.

Reconciliation reviewed the seven PR commits through `816ca8e`; the PR changes three files.
Its published head passes Python 3.12/3.13, lint, format, typecheck, verification, mirror
verification and combined coverage checks. PR #413 is open and ready for review. Its acceptance
criteria are plain bullets; the first two are verified and should be checked. Updating the PR
body, adding `needs-human`, and posting the reconciliation comment were rejected with
`MCP tool call requires approval, but approval policy is never`. Remote tracking is unchanged.
Fresh CI for this change, the seven-minute floor and repository merge_guard remain required.

Black 26.5.1 formatted the changed test and the required repository-wide check passed:
**297 files would be left unchanged**. The tools were staged from upstream GitHub sources in
`/tmp/gate-tools`, using the worker-wakeup workaround above; `git diff --check` passes.
The checkout's read-only `.git` prevents staging or committing on the branch. The source/test
commit is prepared in `/tmp/gate-normalized-verdict-review.git`, with an applyable patch at
`/tmp/gate-normalized-verdict-review.patch`; the same changes remain in the working tree.
