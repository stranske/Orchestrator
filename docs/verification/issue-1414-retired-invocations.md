# Issue #1414: retired invocation evidence

Implemented the read-only `capabilities.invoked_after_retirement` predicate and separate
`retired_but_invoked` entries/counts in the firing monitor and gate-expiry notice. Entries retain
the retirement event, subsequent non-fixture invocation events and the declared kill switch.
Missing or unreadable ledgers report an unmeasured count (`null`), distinct from a measured zero.

The named test uses the existing production heartbeat emitter against a temporary ledger. It
checks both reports and their rendered evidence, silent/fixture/renewed/completion controls,
unchanged live finding lists and population, not-monitored accounting, and ledger immutability.
Additional gate tests cover repeated retirements, later renewal/live transitions, old timestamps,
same-second append ordering, old retirements without expiry, and missing/corrupt/unreadable ledgers.

## Verification on 2026-11-17

The environment has no installed pytest or Black. Installing them failed because pypi.org could
not resolve. The required named pytest command exits before collection:

```text
python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"
No module named pytest
```

Both the targeted Black formatting command and the required repository-wide check are blocked:

```text
black: command not found
```

No commit was made because the mandatory Black gate could not pass. No pytest acceptance checkbox
is marked complete. GitHub API access also failed at connection time, so PR task updates, a remote
evidence comment and verification of the PR's open/ready state remain outstanding.

Successful supplemental checks (these do **not** replace the required pytest/Black commands):

- `python3 src/capability_firing_monitor.py --selftest`, with temporary `ORCH_STATE_DIR`: passed.
- All eight test bodies in `tests/test_firing_monitor_live_rows.py`, imported normally and called
  directly using temporary paths and `unittest.mock` patches: passed, including the named test.
- Standard-library predicate checks: latest retirement, renewal, live transition, fixture,
  completion, field-only, old timestamp, same-second ordering, missing transition, non-retired
  status and immutability passed.
- Standard-library gate checks: missing/corrupt/unreadable/empty ledgers, an old retirement with
  no expiry, undeclared kill switch and ledger immutability passed.
- Python compilation, SVG XML parsing and `git diff --check`: passed.

## Deliberate break and restoration

Temporarily inserted an unconditional `return None` at the start of
`invoked_after_retirement`, then ran the direct test-body checks. The named test failed at its
count assertion, with the retired-invocation evidence empty:

```text
test_invocation_after_retirement_is_named
    assert section["retired_but_invoked_count"] == 1, section["retired_but_invoked"]
AssertionError: []
```

Restored the exact original source bytes in a `finally` block and reran the direct checks. All
eight test bodies passed. This demonstrates that the named test detects the missing predicate;
the formal pytest break/revert acceptance run still needs an environment with pytest installed.

## Remaining required commands

```bash
black --line-length 100 src/capabilities.py src/capability_firing_monitor.py src/switch_review.py tests/test_firing_monitor_live_rows.py tests/test_gate_expiry_renewal.py
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"
python3 -m pytest tests/test_firing_monitor_live_rows.py tests/test_gate_expiry_renewal.py -q -m "not slow"
```

Repeat the deliberate break/revert with the named pytest command, publish the evidence on the
existing PR, verify the corresponding checkboxes, and commit on `claude/issue-1414` after the
formatting and test gates pass.
