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

## Follow-up validation and reconciliation — 2026-11-17

Reviewed the actual changes in `75f3d52`, `76c8930`, and `f66cae4`. The helper,
both report sections, and the controls are committed. The original pytest/Black
availability blocker above was resolved for this run by loading pytest 8.3.5,
Black 24.10.0, and their dependencies from pinned official upstream GitHub
releases into `/tmp/orchestrator-test-tools`; installed pip-vendored dependencies
were copied into that temporary directory. No repository dependency changed.

Verified task reconciliation:

- [x] Read-only `invoked_after_retirement` helper with lifecycle and fixture controls.
- [x] Separate firing-monitor diagnostic preserving live populations and accounting.
- [x] Gate-expiry diagnostic and formatting, including unmeasured evidence.
- [x] Named test and clean, fixture, completion-only, renewed, and missing-ledger controls.
- [x] Named acceptance test passes independently.
- [x] Whole firing-monitor file passes.
- [ ] Publish the verified deliberate-break/restoration evidence in the PR.

This round extends existing cases in `tests/test_firing_monitor_live_rows.py` to
check gate expiry as well as the firing monitor for missing, invalid-JSON, and
invalid-schema ledgers, plus five malformed retirement histories. Valid anomalies
before and after the corrupt row cannot leak as a partial measured count; the
formatted notice remains unmeasured and both readers preserve ledger bytes.
The number of collected tests is unchanged.

Python 3.12.16 commands, using `PYTHONPATH=/tmp/orchestrator-test-tools`:

```text
python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"
1 passed

python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"
25 passed

python3 -m pytest tests/test_firing_monitor_live_rows.py tests/test_gate_expiry_renewal.py -q -m "not slow"
52 passed
```

The six-file regression run also includes `test_firing_monitor_read_only.py`,
`test_firing_monitor_held_off.py`, `test_firing_silence_invocations.py`, and
`test_firing_regressions_section.py`: **104 passed in 2.12s**, with
`-m "not slow"` and `ORCH_STATE_DIR=/tmp/issue-1414-regression-state`.

### Deliberate break and exact restoration

Temporarily inserted an unconditional `return None` after the helper docstring
and ran the named acceptance test:

```text
> assert report["retired_but_invoked_count"] == 1
E assert 0 == 1
FAILED tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named
1 failed in 0.13s
```

A `finally` block restored the original source bytes. The same named command
then reported **1 passed in 0.11s**, and `git diff -- src/capabilities.py` is empty.
The 104-test regression run was performed after restoration and the test edits.

### Remaining delivery blockers

Black formatted and checked the edited test file at line length 100 successfully.
The required full command completed with exit status 1:

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
21 files would be reformatted, 693 files would be left unchanged.
```

All 21 flagged files were compared byte-for-byte with `HEAD` and are unchanged.
The temporary CLI launcher adds periodic asyncio wakeups for this sandbox;
Black's formatter, AST-equivalence checks, and CLI options are unchanged.
No unrelated file was formatted. The mandatory pre-commit gate forbids a commit
while this check fails; Git metadata is also mounted read-only (`os.access`
reports `.git` is not writable). No commit or push was attempted.

Automatic approval review rejected all three requested GitHub mutations: updating
the PR body with verified checkboxes, posting the verification/blocker comment,
and adding `needs-human`. Each returned `MCP tool call requires approval, but
approval policy is never`. Thus the remote checklist and PR evidence remain
unmodified; the unchecked publication task above is a delivery blocker, although
the local deliberate-break/restoration check succeeded.

Follow-up needs the unrelated formatting baseline resolved, writable Git
metadata, and approval to publish this verified reconciliation and evidence.
