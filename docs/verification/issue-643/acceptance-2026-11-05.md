# Issue 643 follow-up verification — 2026-11-05

The implementation and acceptance evidence in commits `9ba9653` through
`89b4ba0` cover the six implementation tasks, the three named private-Brain
tests, and the reverted duplicate-advice negative control. Their PR checkboxes
were stale. The reconciled PR body was prepared, but the GitHub update tool
returned `MCP tool call requires approval, but approval policy is never`; the
remote checklist was not changed.

The repository's `.git` directory is mounted read-only: staging returned
`Unable to create .../.git/index.lock: Read-only file system`. Delivery therefore
uses a commit in a temporary checkout and an exported patch; this run cannot
advance the original branch. PR #1129 was verified open and ready for review.

New integration tests in `tests/test_adjudicator_retro_cli.js` exercise the live
caller through the actual certifier, role adapter, proposal validator, role
recorder, and advice-event writer. GitHub reads, checkout provisioning, backend
selection and the paid provider use local fixtures. The tests cover the default
three-case budget, a two-case override, the zero kill switch, the backend's
600-second timeout, subsequent daily draining without duplicate dispatch,
certified durable receipts, and unchanged original runs, outcomes and influence
edges. They do not constitute production advice.

Fresh results:

```text
node --test --test-isolation=none --test-name-pattern 'live shadow real role adapter' tests/test_adjudicator_retro_cli.js
3 passed, 0 failed

PYTHONPATH=src python -m unittest discover -s tests -p test_adjudicator_live_disputes.py -v
6 passed

PYTHONPATH=src python -m unittest discover -s tests -p test_adjudicator_live_receipts.py -v
1 passed
```

Before adding the new cases, the full CLI test module ran with
`node --test --test-isolation=none tests/test_adjudicator_retro_cli.js`:
65 passed, 2 failed. Both baseline failures were
`retro collects complete gate pages before shadow dispatch`, scenarios
`numeric-run-order` and `numeric-attempt-order` (expected three reads, observed
one). No existing test was disabled or altered.

The existing `adjudicator_live.py --selftest` also failed before these changes:
its second dispute uses `now - 1`, equal to the selection cursor after the first
dispute, so the strict newer-than predicate excludes it. This is a stale smoke
fixture; it is not evidence that an eligible uncertified case was dispatched.
No Python files were changed. Pytest and Black are unavailable on this runner;
installing them failed because the package index could not be resolved. The
named pytest tests were not rerun here; their prior verification remains in
the 2026-11-04 evidence.

The production pass-number criterion remains open: the owner's sync and dated
#575 readback must establish real advised and graded counts and agreement
against 0.75. No production deployment, paid work, or milestone is claimed.
