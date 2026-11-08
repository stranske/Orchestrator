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
