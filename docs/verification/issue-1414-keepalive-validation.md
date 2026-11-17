# Issue 1414 keepalive validation — 2026-11-17

Branch: `codex/issue-1414`. Existing implementation and named acceptance test were
already present at `16bb518c7882deed3112ed4bb00cfe6abe7690d3`.

## Local change

A retired transition without `timestamp` reproduced a `KeyError` from
`invoked_after_retirement`, aborting `capability_firing_monitor.review`.
The monitor now reports unreadable retirement history as unmeasured, discards
partial anomaly counts, and continues live findings and non-live accounting.
Five regression cases cover missing/invalid/null timestamps, malformed metadata,
and a null event, with valid anomalies both before and after the corrupt row.

## Verification

Three direct standard-library unittest checks passed:

- Five malformed-history cases preserve live findings, finding population,
  non-live accounting, and ledger bytes while reporting an unmeasured diagnostic.
- Real production heartbeat recording is reported by both readers; silent,
  fixture, success/failure/outcome-only, pre-retirement completion, renewed,
  live-transition, re-retirement, backdated, and missing-transition controls are
  excluded.
- Missing, invalid-JSON, and invalid-schema ledgers remain unmeasured in both
  readers and their contents are unchanged.

Both changed Python files parse successfully; `git diff --check` passes.
These direct checks do not substitute for the required pytest acceptance runs.

## Blocked requirements

The following commands could not run:

```text
python3 -m black --line-length 100 src/capability_firing_monitor.py tests/test_firing_monitor_live_rows.py
  No module named black

black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
  black: command not found

python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"
  No module named pytest

python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"
  No module named pytest

python3 -m pytest tests/test_firing_monitor_live_rows.py tests/test_gate_expiry_renewal.py -q -m "not slow"
  No module named pytest
```

Installing pytest and Black into `/tmp/orchestrator-test-tools` failed because
`pypi.org` could not resolve. No local installations of either tool were found.
No commit was made because the mandatory Black gate could not run. No task or
acceptance checkbox was marked complete. The named deliberate-break/revert check
remains pending.

Attempts to post the blocker comment and add `needs-human` to PR 1426 were both
rejected by the connector: `MCP tool call requires approval, but approval policy
is never`. A subsequent read verified that the PR is open with `draft=false`.

Follow-up requires pytest and Black in the runner, formatting and the required
checks, the named deliberate-break/revert acceptance check, and a commit of the
local source/test changes on the existing branch.
