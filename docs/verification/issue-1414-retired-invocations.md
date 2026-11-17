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

The code and test verification above succeeded, but the changes remain uncommitted. Local
`git add` failed to create `.git/index.lock` because `.git` is mounted read-only. Creating the
commit through the connected GitHub API was also blocked at `create_tree`:

```text
MCP tool call requires approval, but approval policy is never
```

Posting the verification/blocker comment and adding `needs-human` to PR #1416 were attempted
and rejected by the same approval policy. No PR checkboxes were changed. The requirement to show
the deliberate break/revert evidence on the PR therefore remains outstanding, along with
committing these two files to the existing `claude/issue-1414` branch. A writable checkout or
approved GitHub write capability is required. PR #1416 was confirmed open with `draft=false`.
