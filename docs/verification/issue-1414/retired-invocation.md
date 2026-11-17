# Retirement invocation verification

Verified locally on 2026-11-17 using Python 3.12.16 and pytest 8.3.5. Test tools were
loaded from pinned releases in their official upstream repositories into `/tmp` because
the default interpreter had no pytest or Black and PyPI DNS resolution was unavailable.
No repository dependency or workflow was changed.

```text
python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"
1 passed

python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"
9 passed

python3 -m pytest tests/test_firing_monitor_live_rows.py tests/test_gate_expiry_renewal.py -q -m "not slow"
36 passed
```

Both `capability_firing_monitor.py --selftest` and `switch_review.py --selftest` passed
with temporary state directories. The regression test uses the real invocation heartbeat
writer and verifies that both reports leave the ledger bytes unchanged. It covers silent,
fixture, completion-only, renewed, revived, repeated-retirement and backdated controls.
Additional checks distinguish a measured zero from missing or corrupt ledger evidence.

## Deliberate break and restoration

Temporarily inserted an unconditional `return None` at the start of the helper body.
The exact named pytest test failed at its count assertion:

```text
> assert report["retired_but_invoked_count"] == 1
E assert 0 == 1
FAILED tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named
1 failed in 0.13s
```

Restored the original implementation in a `finally` block and reran the same command:

```text
1 passed in 0.12s
```

The combined 36-test run above was repeated after restoration and passed.

An expanded run including `test_firing_monitor_read_only.py`,
`test_firing_monitor_held_off.py`, `test_firing_silence_invocations.py` and
`test_firing_regressions_section.py` passed all 88 tests after fixing the missing-ledger
guard to respect an injected ledger reader.

## Formatting

Black 24.10.0 formatted all five changed Python files at line length 100. The required
repository-wide check ran to completion and exited 1:

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
21 files would be reformatted, 692 files would be left unchanged.
```

Every one of the 21 flagged files was verified byte-for-byte identical to `HEAD`.
No unrelated file was reformatted. All changed Python files pass Black's check.
The temporary Black launcher used the thread-executor path and periodic event-loop
wakeups to complete asynchronous worker reporting in the restricted sandbox; formatter
and AST checks remained enabled.

## Delivery blocker

The checkout's `.git` directory is mounted read-only. Staging the implementation failed:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

No commit or push was made. Adding the requested `needs-human` label and posting the PR
verification/blocker comment through the GitHub connector were also rejected because
those actions require approval and this run's approval policy is `never`. The
implementation and test changes remain in the working tree. PR #1426 was verified open
and ready for review (`draft: false`). Task checkboxes remain open pending delivery.
