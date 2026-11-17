# Issue #1414: retired invocation evidence

The read-only `capabilities.invoked_after_retirement` predicate and separate
`retired_but_invoked` entries/counts are implemented in the firing monitor and gate-expiry notice.
Entries retain the retirement event, subsequent non-fixture invocation events and declared kill
switch. Missing or unreadable ledgers report an unmeasured count (`null`), distinct from zero.

The named test uses the production heartbeat emitter against a temporary ledger. It checks both
reports and rendered evidence, silent/fixture/renewed/completion controls, unchanged live findings
and population, not-monitored accounting, and ledger immutability. It also verifies that renewal
and live-transition events cancel the old retirement even with stale retired status, and that a
later clean retirement excludes invocations from the preceding retirement window.

The firing-history test now isolates both `STATE_DIR` and `HISTORY`: redirecting the history file
alone left `record()` trying to create the production state directory in this read-only runner.

## Verification on 2026-11-17

The initial targeted run reproduced the test isolation problem: 52 passed, one failed with
`OSError: [Errno 30] Read-only file system: '/home/runner/.codex/orchestrator'`.
After isolating the state directory, the required commands succeeded:

```text
python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"
1 passed

python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"
8 passed

python3 -m pytest tests/test_firing_monitor_live_rows.py tests/test_gate_expiry_renewal.py -q -m "not slow"
53 passed
```

Formatting also passed with the CI-pinned Black 26.10.1:

```text
black --line-length 100 tests/test_firing_monitor_live_rows.py
1 file left unchanged.

black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
712 files would be left unchanged.
```

Both commands exited zero; `git diff --check` also passed.

## Deliberate break and restoration

Inserted an unconditional `return None` immediately inside `invoked_after_retirement`, then ran
the named pytest command above. The real report contained zero retired-but-invoked entries and
the test failed at its count assertion:

```text
baseline exit: 0
1 passed in 0.11s
deliberate-break exit: 1
E           AssertionError: []
E           assert 0 == 1
1 failed in 0.15s
restored exit: 0
1 passed in 0.13s
```

Restored the exact original source bytes in a `finally` block before rerunning the test. The
restored `src/capabilities.py` SHA256 was
`fe2f50db027529e8abdd1690050cc786a8aa91d19dcfbc235b27e59775c3f8c3`.
The deliberate break is absent from the restored implementation.

## Test tools

PyPI DNS resolution failed and the runner had no pytest or Black. Official upstream sources were
staged under `/tmp/issue-1414-tools26` through the connected GitHub reader; no tool dependencies or
bootstrap files were added to the repository. Commands used CPython 3.12.16, pytest 8.3.5
(`pytest-dev/pytest` at `b55ab2aabb68c0ce94c3903139b062d0c2790152`) and the CI-pinned Black 26.10.1
(`psf/black` at `258e7dbf348bc23cb70b8cdf693481c04634ebcc`), with official dependencies and generated
package version metadata. Prepending `/tmp/issue-1414-tools26/bin` to `PATH` selects these tools.

## Delivery blocker

The previous iteration's test isolation and verification changes were subsequently committed in
`99bf287`. This round reviewed `9dcfc46`, `78301a4`, and `99bf287` and confirmed that the helper,
both report sections, and the requested controls were already implemented. The first unchecked
helper task can be reconciled against that implementation and the passing tests.

This round strengthens the named test with mixed production and fixture invocation events on
the same retired row. Both reports must retain both production invocations (including one with
no source metadata), exclude the fixture invocation, and count one anomalous capability.

Fresh verification against this change, with `-m "not slow"` throughout:

```text
named test baseline:       1 passed in 0.11s
unconditional return None: 1 failed in 0.15s (AssertionError: []; assert 0 == 1)
exact-byte restoration:    1 passed in 0.12s
whole firing-monitor file: 8 passed in 0.13s
both relevant test files:  53 passed in 0.64s
```

The break probe restored the source in a `finally` block, removed its compiled bytecode, and
verified the same source SHA256 recorded above. Its full transcript is retained locally at
`/tmp/issue-1414-break-revert.txt`.

Black 26.10.1 formatted the changed test file successfully (`1 file left unchanged`), and
`git diff --check` passed. Initial repository-wide attempts stalled, and a diagnostic retry
exited 139 while reporting `blib2to3/pytree.py:update_sibling_maps`. Checking the tracked Python
files individually with Black's CLI completed successfully: `712 files checked; 0 failures`.
This populated Black's normal cache from successful checks. The required repository-wide command
then exited zero with `712 files would be left unchanged`:

```text
python3 -m black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

This round used CPython 3.12.16 and the official upstream sources staged at
`/tmp/issue-1414-tools` (`PYTHONPATH=/tmp/issue-1414-tools`), with
`BLACK_NUM_WORKERS=1` and `BLACK_CACHE_DIR=/tmp/issue-1414-black-cache`. The individual-check
result is retained at `/tmp/issue-1414-black-individual.log`.

Task reconciliation from the implementation and fresh test evidence:

- [x] Read-only helper examines the latest unrenewed retirement and excludes fixtures.
- [x] Firing monitor separately reports retirement anomalies without changing live findings.
- [x] Gate expiry and its formatter name anomalies and distinguish unmeasured from zero.
- [x] Named test and clean, fixture, completion, renewal, and missing-ledger controls exist.
- [x] Required named test passes.
- [x] Whole firing-monitor test file passes.
- [ ] Deliberate break/restoration evidence is shown on the PR (verified locally; posting blocked).

The current test change remains uncommitted. Local `git add` failed to create `.git/index.lock`
because `.git` is mounted read-only. Updating the PR body to reconcile the helper checkbox and
adding `needs-human` were rejected by the connected GitHub write tools:

```text
MCP tool call requires approval, but approval policy is never
```

Posting the verification and blocker comment was also attempted and rejected by the same policy.

No PR checkboxes were changed. The requirement to show the deliberate break/revert evidence on
the PR therefore remains outstanding, along with committing the test and this verification note
to the existing `claude/issue-1414` branch. A writable Git directory and an approved GitHub write
capability are required. PR #1416 was confirmed open with `draft=false`.

## Current iteration: ordering and unreadable-ledger controls

Reviewed the recent commits through `d3ce201`; the previously uncommitted changes described above
are now in that commit. The helper task remains unchecked on the PR despite the implementation
and passing tests. Attempting to reconcile it through the GitHub connector was rejected again.

This iteration strengthens the named test with an invocation appended before retirement in the
same second. The helper tests also exclude a future-dated invocation appended before retirement.
The firing-monitor measurement control now covers a directory at the ledger path, requiring
`unmeasured` and verifying the directory survives the read unchanged.

Fresh verification used CPython 3.12.16 and pytest 8.3.5, with `-m "not slow"` on every pytest run:

```text
named test baseline:       1 passed in 0.22s
unconditional return None: 1 failed in 0.24s (AssertionError: []; assert 0 == 1)
exact-byte restoration:    1 passed in 0.22s
whole firing-monitor file: 8 passed in 0.27s
both relevant test files:  55 passed in 1.11s
```

The mutation probe restored the original source in a `finally` block and verified the same
SHA256 recorded above. The transcript is at `/tmp/issue-1414-break-revert-current.txt`.
Black 26.10.1 formatted each changed test file separately; both were already formatted.
Trusted upstream test/formatter sources were staged under `/tmp/issue-1414-tools` because this
fresh runner lacked those tools and PyPI DNS resolution failed. No dependencies were added to
the repository.

The PR-body update, verification comment, and `needs-human` label were each rejected with
`MCP tool call requires approval, but approval policy is never`. Consequently the PR checkboxes
are unchanged and the requirement to show the mutation evidence on the PR remains incomplete.
Git still cannot create `.git/index.lock` because `.git` is mounted read-only, so the new test
changes require a writable Git directory before they can be committed to the checked-out branch.

The repository-wide Black run initially timed out. Running each tracked Python file through
Black's CLI individually in one interpreter completed successfully: `712 files checked;
0 failures`. The required repository-wide command then passed from Black's validated cache:

```text
python3 -m black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
712 files would be left unchanged.
```

Both checks used Black 26.10.1, `BLACK_NUM_WORKERS=1` and
`BLACK_CACHE_DIR=/tmp/issue-1414-black-cache`. The individual-check transcript is at
`/tmp/issue-1414-black-individual-current.log`; `git diff --check` also passes.
PR #1416 remains open and ready for review (`draft=false`).
