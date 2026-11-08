# Issue #1250: capacity fixture admission deadline

## Latest local pytest verification (2026-11-08)

Pytest is now available for this run through pinned official upstream sources
staged only in `/tmp/issue1250-tools`. No repository dependency files changed.
The earlier missing-tool reports below describe previous attempts.

The original pytest case now also retries while capacity remains zero. It
requires a fresh sample at logical time `1800000030`, a retained target `a`,
`worker_capacity` / `capacity_recheck` deferral, no action, and valid stop
evidence. It then restores capacity and requires exactly one completed action.
No collected cases, collection floors, or shard definitions changed.

Verified commands (`PYTHONPATH=/tmp/issue1250-tools`):

```text
python3 -m pytest tests/test_closer_pipeline.py::test_capacity_is_read_fresh_and_zero_defers -m 'not slow' -q
1 passed

python3 -m pytest -p pytest_cov.plugin tests/test_closer_pipeline.py --cov=closer_pipeline --cov-report=term-missing -m 'not slow' -q
252 passed
src/closer_pipeline.py    2268    818    63.9%

python3 -m pytest --collect-only -q -m 'not slow'
15073 tests collected

node --test --test-isolation=none --test-reporter=tap tests/test_closer_pipeline_capacity_clock.js
6 passed
```

In the isolated checkout `/tmp/issue1250-review`, the actual named pytest case
failed with `KeyError: 'a'` when either the helper's deadline, admission clock,
or both reverted to wall time. Restoring the fixture passed after each of the
three mutations. The traceback identifies the failing lookup:

```text
tests/test_closer_pipeline.py:1586: in test_capacity_is_read_fresh_and_zero_defers
    row = receipt["targets"]["a"]
E   KeyError: 'a'
```

A separate temporary production mutation froze the capacity sample timestamp
at `1800000000`. The extended pytest case failed against the retry's expected
`1800000030`; restoring production passed. No mutation remains in either
checkout.

The broader command `python3 -m pytest tests/ -m 'not slow' -x -q` stopped
with **685 passed, 6 skipped, 1 failed**. Its first failure is unrelated to the
capacity fixture:
`tests/test_agent_gh_guard.py::test_an_inherited_bash_env_still_runs_under_the_guard`
expected inherited `BASH_ENV` output `ran` but received empty stdout. This
failure is outside issue #1250's scope; no guard code was changed.

- [x] Execute the original named pytest case and complete pipeline pytest module.
- [x] Reproduce the missing-target failure against the actual pytest fixture and
  verify that restoring the fixture passes.
- [ ] Verify the repository aggregate and combined coverage without regressions.
- [ ] Verify regenerated sync PR #1247 and the next sync-core classification.

The targeted coverage measurement is not the aggregate/combined coverage gate.
The PR's existing checked aggregate-verification box remains unsupported.
An attempt to uncheck that box through the GitHub connector was rejected:
`MCP tool call requires approval, but approval policy is never`. The remote PR
body was not changed.

The required formatting gate passed with the CI-pinned Black **26.10.1**:
`black --line-length 100 tests/test_closer_pipeline.py` left the file unchanged,
and `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`
reported **1014 files would be left unchanged**, exit 0. An executor-shutdown
hang in this sandbox required checking every discovered file first through
Black's unmodified synchronous single-file API, then running the exact CLI
command against that populated cache. Formatting rules and exclusions were
unchanged.

Workspace staging remains blocked by the read-only `.git/index.lock` path.
The tested changes are committed in `/tmp/issue1250-review`, with an applyable
patch at `/tmp/issue1250-pytest-validation.patch`; the workspace retains the
same changes for automation collection.

The final remote read shows PR #1251 merged at `2026-11-08T18:59:16Z` with
head `bf2fd9f058e9212f03d41266ae420c885e0f8a5e`, before this follow-up commit.
This round did not merge or otherwise mutate the PR. The additional pytest
assertions and this verification update remain local and are available in the
exported patch; they are not part of that merged head.

The original `test_capacity_is_read_fresh_and_zero_defers` calls the test helper
`run(..., seconds=0.06)` using wall time. A scheduling pause before
`closer_pipeline._run_pipeline_impl` checks admission can exhaust that budget.
The pipeline correctly returns `reason="admission_deadline_expired"` with
`targets={}`. The test's subsequent `receipt["targets"]["a"]` lookup raises
`KeyError: 'a'`; it has not reached capacity admission.

## Local reproduction and direct validation (2026-11-08)

A standard-library harness called the real `run_pipeline` with the original
60-millisecond deadline, after a 100-millisecond sleep to model pre-admission
scheduling delay. Its receipt and original assertion produced:

```text
Delayed original receipt: admission_deadline_expired {}
Traceback (most recent call last):
  File "<stdin>", line 21, in <module>
KeyError: 'a'
```

Repeating with `now=lambda: 1_800_000_000.0` passed the capacity assertions:
zero capacity defers with reason `worker_capacity`, the next trigger is
`capacity_recheck`, and the recorded IO capacity sample has zero available
workers. Updating the capacity mapping to one worker per lane and invoking
the same state again completed target `a` exactly once. `validate_stop` passed
after both invocations. This is direct production-code validation, not a pytest
run or an aggregate verification result.

The revised existing test uses that logical clock and asserts both deferral
evidence and successful resumption with fresh capacity. It adds no collected
test cases, so the collection floor and shard plan need no changes.

## Outstanding validation

The default Python has no pytest or Black. Installing them into a temporary
environment failed because package-index DNS/network access is restricted.
The targeted pytest/coverage command and both required Black commands could
not run. No commit was made because the mandatory Black gate has not passed.
No task checkboxes were marked complete.

Attempts to post a blocker comment and add `needs-human` to PR #1251 were
rejected: the GitHub mutation tools require approval and this run's approval
policy is `never`. The PR was re-read and remains open with `draft=false`.

Run the named test and `tests/test_closer_pipeline.py` under pytest, then the
repository's shard/aggregate verification with combined coverage, in an
environment with the test tools installed. Run the required Black formatting
and repository check before committing. Regeneration of sync PR #1247 and the
next sync-core classification remain external post-merge checks.

## Regression follow-up (2026-11-08)

The existing pytest case now patches wall time and wraps the pipeline call to
advance that clock by five seconds after the helper computes its deadline.
This models the admission delay without sleeping and adds no collected cases.
Both the capacity deferral and fresh-capacity resumption retain the independent
logical clock, so restoring wall-clock admission would fail deterministically.

A separate standard-library check against the production pipeline reproduced
`admission_deadline_expired`, empty targets, and the original `KeyError: 'a'`
under that simulated delay. Restoring the logical clock passed the zero-capacity
evidence assertions, successful resumption with one action, and both stop
validations. This check is not execution of the pytest case or the aggregate.

Pytest and Black remain unavailable. Installation into a temporary virtual
environment found no available distributions, and both required Black commands
exit 127. The regression change remains uncommitted under the mandatory
formatting gate; no new task checkboxes are complete. The existing PR checkbox
claiming successful aggregate verification is not supported by these results.
Posting the blocker comment and adding `needs-human` were again rejected because
the GitHub tools require approval and the approval policy is `never`.

## Clock-source regression follow-up (2026-11-08)

The existing case now retains the original 60-millisecond budget and starts wall
time 60 seconds behind the injected logical clock. The wrapper advances wall
time to 60 seconds ahead before production admission. This detects either half
of the helper reverting independently: a wall-time deadline is already expired
on the logical clock, and a wall-time admission check sees the logical deadline
as expired. The previous equal-clock starting point did not detect a helper
that computed only the deadline from wall time. Resumption advances the logical
clock by 30 seconds and repeats the wall-clock crossing against the same state.
No collected cases or collection floors changed.

A standard-library harness called the real `run_pipeline` with these clock
values, the existing callback shape, and the original budget. Results:

```text
wall deadline=False, wall admission=False: PASS deferral, fresh-capacity resumption, exactly one action, both stop validations
wall deadline=True, wall admission=False: reproduced KeyError: 'a'
wall deadline=False, wall admission=True: reproduced KeyError: 'a'
wall deadline=True, wall admission=True: reproduced KeyError: 'a'
```

These are direct production-code checks, not execution of the pytest case or
the requested pytest break-and-revert acceptance procedure. AST parsing and
`git diff --check` pass. The targeted command
`python3 -m pytest tests/test_closer_pipeline.py --cov=closer_pipeline --cov-report=term-missing -m 'not slow'`
fails with `No module named pytest`. Both required Black commands fail with
`black: command not found` (exit 127). No cached pytest, Black, or coverage
installation was found; installation into `/tmp/issue-1250-tools` from both
the configured index and official PyPI found no available distributions.

The required formatting gate still blocks committing Python changes. No task
checkboxes were marked complete, and aggregate/combined coverage and the live
sync acceptance checks remain unverified.

Posting the updated blocker comment and adding `needs-human` to PR #1251 were
both rejected again: `MCP tool call requires approval, but approval policy is
never`. Neither remote mutation succeeded.

## Executable Node regression (2026-11-08)

`tests/test_closer_pipeline_capacity_clock.js` now exercises the production
`run_pipeline` and `validate_stop` APIs through `node --test` and the bundled
Python interpreter. It needs no third-party Python packages. Each subprocess
owns a temporary state directory and has a 15-second hang guard. The test keeps
the original 60-millisecond budget and models the pre-admission scheduling pause
with separate logical and wall clocks, without sleeping.

Validation command (process isolation is disabled so this sandbox reports each
individual Node test):

```text
node --test --test-isolation=none --test-reporter=tap tests/test_closer_pipeline_capacity_clock.js
# tests 4
# pass 4
# fail 0
```

The positive case verifies zero-capacity deferral, the exact IO capacity sample,
no premature action, a fresh-capacity retry against the same state, exactly one
completed action, and both stop validations. Three negative controls independently
restore wall-clock deadline calculation, wall-clock admission, or both. Each must
exit 1 with `admission_deadline_expired`, empty targets, and this traceback:

```text
Traceback (most recent call last):
  File "<string>", line 59, in <module>
    deferred = first["targets"]["a"]
               ~~~~~~~~~~~~~~~~^^^^^
KeyError: 'a'
```

Each negative control then restores the logical-clock fixture and requires all
positive assertions to pass again. No broken source is left in the checkout.

- [x] Add and execute a regression test using the permitted `node --test` runner.
- [ ] Run the original named pytest case and the complete pipeline pytest module.
- [ ] Verify the repository aggregate and combined coverage.
- [ ] Verify regenerated sync PR #1247 and the next sync-core classification.

The last three checks remain unverified. The targeted pytest command still fails
with `No module named pytest`; the required Black check exits 127 because Black
is missing. Installing pytest, pytest-cov, and Black into `/tmp` failed against
both the configured package index and official PyPI; a direct PyPI request confirms
DNS resolution is unavailable. This round changes a JavaScript test and this
verification record, with no `.py` edits. The successful Node regression does not
substantiate the existing PR checkbox claiming aggregate verification passed.

The workspace's `.git` directory is mounted read-only: staging fails while
creating `.git/index.lock`. The tested JavaScript and this record are committed
in an isolated checkout at `/tmp/issue-1250-commit`; an applyable commit patch is
exported to `/tmp/issue-1250-capacity-clock.patch`. The shared workspace retains
the same reviewed changes for the automation's normal collection step.

## Logical deadline boundary follow-up (2026-11-08)

The executable Node regression now also checks admission exactly at and after
the logical deadline, with wall time still behind it. Both cases must return
`admission_deadline_expired`, empty targets, and no authority before sampling
capacity, calling preparation or action, or creating state or its runlock. These
checks complement the successful logical-clock capacity fixture: injecting a
clock must preserve actual deadline expiry as well as prevent wall-time flakes.

The existing Node command above now reports **6 tests, 6 passed, 0 failed**.
In `/tmp/issue-1250-expiry-check`, changing only the production admission
comparison from `deadline <= observed_at` to `deadline < observed_at` made the
`logical-at-deadline` test fail: a runlock was incorrectly created. Restoring
the production file returned all six tests to passing. Its restored SHA-256
matches the workspace source:
`1c30ed3a370932674faf0344fb1c02be42f125a2ae2e986e8623acebda82ba9d`.
No production mutation remains in either checkout.

The original named pytest case and the targeted pipeline pytest/coverage command
still fail to start with `No module named pytest`. Black is also absent, and
installing pytest, pytest-cov and Black into `/tmp` found no distributions.
No Python files were changed in the final diff. Aggregate/combined coverage and
both live sync checks remain unverified; their task checkboxes must stay open.

Staging in the workspace fails with `Read-only file system` for
`.git/index.lock`. The JavaScript test and this record are committed in the
isolated checkout at `/tmp/issue-1250-expiry-check`; the commit patch is
`/tmp/issue-1250-expiry-check.patch`. The workspace contains the same changes for
the automation collection step.

## Repeated capacity deferral regression (2026-11-08)

The existing Node scenario now retries once while capacity remains zero, before
making workers available. Each invocation crosses the logical deadline with wall
time. The retry must retain target `a`, defer for `worker_capacity` with trigger
`capacity_recheck`, record a new capacity sample at logical time `1800000030`,
execute no action, and pass `validate_stop`. The original receipt must retain its
sample at `1800000000`. The subsequent capacity recovery completes exactly one
action and passes stop validation.

Validation:

```text
node --test --test-isolation=none --test-reporter=tap tests/test_closer_pipeline_capacity_clock.js
# tests 6
# pass 6
# fail 0
```

In `/tmp/issue-1250-fresh-capacity`, a temporary production mutation replaced the
zero-capacity receipt's sample timestamp with the first round's timestamp.
The prior six Node tests all passed against that mutation. The extended suite
failed, showing actual `sampled_at: 1800000000` versus expected
`sampled_at: 1800000030`. Restoring production code returned all six cases to
passing. The restored Python source matches the workspace byte for byte. The
mutation logs are `/tmp/issue-1250-fresh-capacity/stale-capacity-red.log` and
`/tmp/issue-1250-fresh-capacity/fresh-capacity-green.log`.

The targeted command
`python3 -m pytest tests/test_closer_pipeline.py --cov=closer_pipeline --cov-report=term-missing -m 'not slow'`
still fails with `No module named pytest`. Installing pytest, pytest-cov and
Black into `/tmp/issue-1250-test-tools` found no distributions. Aggregate and
combined coverage remain unverified. PR #1247 still has its original head
`7ba7006dc3243a7bf0221d709e4de5562c89f042`; regeneration and the next sync-core
classification remain external checks. No outstanding validation checkbox was
marked complete.

Only the JavaScript regression and this record changed. The workspace's Git
directory remains read-only. The commit is prepared in
`/tmp/issue-1250-fresh-capacity`, with its patch exported to
`/tmp/issue-1250-fresh-capacity.patch` for application outside the sandbox.

Posting the blocker comment and adding `needs-human` to PR #1251 were rejected
because the GitHub tools require approval and the approval policy is `never`.
Neither remote mutation succeeded.
