# Issue #1250: capacity fixture admission deadline

## Integrated acceptance record — 2026-11-08

This section supersedes the current-status statements in the historical notes
below. Merged PR #1251 is preserved; it is implementation evidence, not evidence
that every acceptance criterion completed. In particular, its checked claim
that aggregate verification passed **locally** was unsupported by those notes.
The runtime and Git restrictions recorded there describe earlier attempts.

Acceptance outcome: **0 (incomplete)** until the named fixture, pipeline-module
coverage, Node regression, repository aggregate/combined coverage, regenerated
#1247 named check, and subsequent sync-core classification all have observed
receipts. Check date: **2026-11-08**. Start: **19:19:50 UTC**. Stop rule recorded
before validation: **one validation pass and at most one focused correction,
within 20 minutes (19:39:50 UTC)**. Missing receipts do not justify code changes.

Integrated source: `f141cf330a4a303276e7b04d31bc9e3a73d77ca0` (merged #1251),
equal to fetched `origin/main` at validation start and at 19:26 UTC. The supplied
branch is `closer-r1/issue-1250`. `git branch --list 'closer*/issue-1250'`
found only this branch, with no commits absent from main: no prior repair commits
needed merging, and none were discarded. Both productive test files remain
unchanged. No repository virtual environment was selected; commands use
`/opt/anaconda3/bin/python3` (Python 3.12.2).

### Commands and observed results

```sh
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/bin/python3 -m pytest -p no:cacheprovider tests/test_closer_pipeline.py::test_capacity_is_read_fresh_and_zero_defers
# exit 0; 1 passed in 0.89s

/opt/anaconda3/bin/python3 -m pytest tests/test_closer_pipeline.py --cov=closer_pipeline --cov-report=term-missing -m 'not slow'
# exit 0; 252 passed in 495.24s (0:08:15); closer_pipeline coverage 63.8% (2268 statements, 820 missed).
# This required module run exceeded two minutes; no unrelated speed or implementation change was attempted.

PYTHON=/opt/anaconda3/bin/python3 node --test --test-isolation=none --test-reporter=tap tests/test_closer_pipeline_capacity_clock.js
# exit 0; tests 6; pass 6; fail 0; duration_ms 972.151291
```

The Node run includes repeated zero-capacity sampling and recovery, both logical
deadline boundaries, and all three wall-clock negative controls with restored
logical-clock passes. No new deliberate production mutation was needed.

### Actual CI aggregate

`.github/workflows/ci.yml`, job `verify` (display name
`verify.py aggregate + combined coverage`), executes:

```sh
python3 src/verify.py --aggregate shard-evidence --coverage
# pull_request events additionally use --floor-may-lag
```

Live readback:

```sh
gh run view 42512699271 --repo stranske/Orchestrator --json headSha,jobs
# headSha: f141cf330a4a303276e7b04d31bc9e3a73d77ca0
# verify.py aggregate + combined coverage: completed / success
# job 128831067478
# verify.py aggregate in the flat exec-mirror shape: completed / success
# job 128830626843

gh api repos/stranske/Orchestrator/actions/jobs/128831067478/logs
# pytest: 15042 passed, 0 failed, 31/31 max (15075 collected; floor 15075)
# selftests: 117 of 124 modules ran, 7/7 max skipped
# gates: 2/2 max skipped
# coverage: combined 141 instrumented process(es)
# TOTAL 121053 14714 87.8%
```

These are successful named jobs on the integrated source, not a claim that the
regenerated #1247 passed. The integrated CI workflow later completed successfully. [Integrated aggregate job](https://github.com/stranske/Orchestrator/actions/runs/42512699271/job/128831067478).

Local aggregate attempt: unmodified `pytest-shard-*` artifacts were downloaded
from this exact run with `gh run download 42512699271 --repo
stranske/Orchestrator --pattern 'pytest-shard-*' --dir
/tmp/issue-1250-r1-evidence/shards` and flattened without modifying their contents.
A scratch checkout at the same source revision and the CI-selected Workflows Core
source `be8c28da2cbc181dcdec2be30f71415ccce43cf9` were used, with private empty
`ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME`, `ORCH_VERIFY_JOBS=4`, and
`PYTHONDONTWRITEBYTECODE=1`. Command:

```sh
/opt/anaconda3/bin/python3 src/verify.py --aggregate /tmp/issue-1250-r1-evidence/shard-evidence --coverage
# exit 124; TIMEOUT: aggregate exceeded 120-second validation bound; process group stopped.
# No summary or local success was emitted; no focused source correction is justified.
```

No local full pytest suite or shard was run. The aggregate consumes CI shard
receipts and executes its configured selftests/gates; it does not rerun pytest.

### Regeneration and classification remain open

```sh
gh run view 42500846328 --repo stranske/Orchestrator --json databaseId,headSha,jobs
# headSha: 7ba7006dc3243a7bf0221d709e4de5562c89f042
# verify.py aggregate + combined coverage: completed / failure
# job 128795407800
```

#1247 remains at that original head. The successful integrated-main job above
cannot satisfy the regenerated-PR criterion. Its generated branch has not been
edited by this repair.

A scoped request to the existing Maint 71 owner used `repos=stranske/Orchestrator`,
`auto_merge=false`, `cleanup_branches=false`, `dry_run=false`:

- Run [42514849833](https://github.com/stranske/Workflows/actions/runs/42514849833)
  rejected the plan-ID selector before acting: `active sync selector must be
  candidate, campaign, delivery, dev-tool, or a legacy hex hash`.
- The single input correction used the recorded template hash `a478e45c16b6`.
  Run [42514935317](https://github.com/stranske/Workflows/actions/runs/42514935317)
  failed with `target_missing`: it expected `sync/workflows-a478e45c16b6` while
  #1247 uses `sync/workflows-delivery`. It reported 0 merged, 0 stale closed,
  and 0 branches deleted. No further dispatch retry or configuration change.

Readback commands: `gh run view 42514849833 --repo stranske/Workflows
--log-failed`, `gh run view 42514935317 --repo stranske/Workflows --log-failed`,
and `gh pr view 1247 --repo stranske/Orchestrator --json headRefOid,updatedAt`.

The existing sync-core target receipt at
`~/.codex/orchestrator/sync-core/targets/stranske_orchestrator_1247.json`
records round `20261108T191552Z`, disposition `pre-existing`, terminal
`handed-off`, on the original head. It is not a post-regeneration classification.
The subsequent live round `20261108T192823Z` still observed the same original
head and classified it `shipment-caused`, owner `stranske/Workflows`. Its row
reports `verify.py aggregate + combined coverage: classifier says shipment-caused`
and an existing-owner handoff [Workflows #4555](https://github.com/stranske/Workflows/issues/4555).
That recorded classifier claim is not proof of a new shipment defect: the
candidate has not incorporated the passing integrated revision. It does not
satisfy the requested clean classification after regeneration. The raw row also
retains `delivery_not_sealed` and an unclassified shard failure; this repair does
not expand into those findings.

No source owner, regeneration configuration, pins, review floor, or merge guard
was changed. Blocking/drainable pair: **#1247 regeneration and subsequent
classification unobserved / existing sync workflow owner can regenerate it;
this repair can read exact-head receipts without taking its branch**.

- [ ] Execute the changed pytest fixture, pipeline module coverage command, and
  repository aggregate/combined-coverage check successfully on the integrated revision.
- [ ] Record successful named-check evidence for regenerated #1247 using
  `gh run view <run-id>` and the subsequent sync-core classification.
- [x] Replace unsupported completion claims with exact commands, revisions,
  run IDs, and observed results; retain unobserved criteria as open.

## Historical execution notes (superseded status)

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
