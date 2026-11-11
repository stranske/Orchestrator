# Issue #1098: local-loop admission mismatch

Reconciliation reviewed the receipt/ledger changes through `9706a965`, including
`add2ebbd` and `215996cc`. None establishes a complete original task or acceptance
criterion. All checkboxes remain unchecked. The PR-body reconciliation update,
blocker comment and `needs-human` label were rejected by the connector with
`MCP tool call requires approval, but approval policy is never`.

The [owner's host receipt](https://github.com/stranske/Orchestrator/issues/1098#issuecomment-6773279526)
identifies a local-loop caller/heartbeat declaration mismatch among the remaining
native audit failures. This run reproduced a source defect behind that finding:
`local-loop` declares an external controller but its entrypoint is the repository's
`local_loop.py:main tick --apply`. `external_caller()` rejected that entrypoint
with `declared external entrypoint must be outside this tree`, in both checkout
and flat-mirror layouts, even with an existing caller and fresh invocation event.

The working-tree change explicitly declares `entrypoint_scope: local` for that
caller. This scope validates a local module and top-level function, rejects path
escapes and requires the caller to resolve outside the checkout. Unscoped external
declarations retain their existing restriction. No recent invocation is fabricated;
missing, stale, future, non-invocation and counterfactual evidence still fails.

Validation on this hosted runner:

- The two-layout standalone stdlib reproduction failed before the change and
  passed afterward. Five further stdlib checks passed for the audit row, caller
  restrictions, entrypoint validation and heartbeat controls. These checks are
  local source regressions, not deployment acceptance receipts.
- Both `src/capability_activation_audit.py --selftest` and
  `src/capabilities.py --selftest` exited zero with temporary runtime paths.
- The existing pytest case now exercises both layouts and negative controls;
  no test functions or parametrization were removed or added. The targeted
  `pytest ... -m "not slow"` attempt could not execute because pytest is absent.
- Syntax parsing and `git diff --check` passed. Black is absent; the required
  formatting and repository-wide check commands could not execute. Installing
  pytest and Black failed with no available distribution. No Python commit is
  permitted until the formatting gate passes.
- `.verify-floor.json` is byte-equal to `origin/main`; SHA-256 is
  `12cea5203329d538fc12312c9f48b7f08686b2efd2c82c41115dd46365bcd1f6`.
- The working-tree `python3 -u src/verify.py` attempt, using temporary runtime
  paths, reached its five-minute timeout and required termination (exit 137).
  Its redirected `/tmp/issue1098-full-verify.log` contains no aggregate report.
  This is not a completed verifier run or a deployment diagnosis.
- `git add` also failed because `.git/index.lock` cannot be created on the
  read-only filesystem. No commit was created or remote delivery attempted.

The deployment mirror, live runtime and complete native aggregate logs are absent
here. Missing opener/sync invocation evidence and ambient feature findings still
require host investigation. No original task, full-verifier acceptance, native
deployment receipt or seven-day collision criterion is claimed complete. PR #1351
was checked open with `isDraft=false`.
