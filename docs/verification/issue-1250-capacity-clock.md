# Issue #1250: capacity fixture admission deadline

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
