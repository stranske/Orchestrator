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

## Real lifecycle writer regression follow-up — 2026-11-17

Extended two existing tests without changing collection counts:

- The named acceptance test now revives the invoked row through `transition`,
  records a live invocation, and retires it again. Both readers exclude the old
  and fixture invocations, then report two new production invocations as one
  capability with only the new retirement evidence. Every report preserves the
  ledger bytes.
- The gate-expiry control now calls `renew` on an expired, invoked row and proves
  that the diagnostic clears and stays clear after a new invocation.

Using Python 3.12.16, pytest 8.3.5 and Black 24.10.0 from pinned official upstream
sources in `/tmp/orchestrator-test-tools`, with all pytest runs selecting
`-m "not slow"` and `ORCH_STATE_DIR=/tmp/issue-1414-validation-state`:

```text
Named acceptance test: 1 passed in 0.22s (after source restoration)
tests/test_firing_monitor_live_rows.py: 25 passed in 0.28s
tests/test_gate_expiry_renewal.py: 27 passed in 0.81s
Six-file targeted regressions listed above: 104 passed in 4.38s
Edited files, Black line length 100: 2 files would be left unchanged
git diff --check: passed
```

The deliberate break inserted an unconditional `return None` in
`invoked_after_retirement`. The named test failed at
`assert report["retired_but_invoked_count"] == 1` with `E assert 0 == 1`
(`1 failed in 0.23s`). A `finally` block restored the original bytes; the named
test and both complete files passed afterward. `src/capabilities.py` was compared
byte-for-byte with `HEAD` after restoration and is unchanged.

The mandatory full Black command again exited 1: **21 files would be reformatted,
693 left unchanged**. Every flagged file was compared byte-for-byte with `HEAD`
and is unchanged. `.git`, its index, objects and branch refs are read-only.
No commit or push was attempted: the mandatory formatting gate forbids it.
The two test improvements remain local.

Automatic approval review rejected publishing the PR evidence comment, adding
`needs-human`, and updating verified checkboxes, each with
`MCP tool call requires approval, but approval policy is never`. The remote
checklist remains unchanged, and publication of the deliberate-break evidence
remains incomplete. PR 1426 was verified open and ready (`draft=false`).

## Mixed-population validation — 2026-11-17

Extended the existing population test in `tests/test_firing_monitor_live_rows.py`
with a retired invocation beside actual live `never_fired`, `overdue`, and
`regressed` findings and a stored snapshot. It verifies retirement/invocation
evidence and the kill switch while preserving the live findings, declared
population, non-live accounting, counts, snapshot bytes, ledger bytes, and the
monitor's production heartbeat call. This extends an existing collected test;
the collection floor is unchanged.

Verified with Python 3.14.10 and pytest 9.0.2 from official upstream sources in
`/tmp`, with `-m "not slow"` on every pytest invocation:

```text
Named acceptance test: 1 passed
tests/test_firing_monitor_live_rows.py: 25 passed in 0.28s
tests/test_gate_expiry_renewal.py: 27 passed
Both complete files after the final test edit: 52 passed in 1.15s
git diff --check: passed
```

The deliberate break temporarily inserted an unconditional `return None` at the
start of `invoked_after_retirement`. The named test failed with `assert 0 == 1`
(`1 failed in 0.25s`, exit 1), demonstrating the zero retired count. A `finally`
block restored the original source bytes; the identical named command then
reported `1 passed in 0.21s`, exit 0. The final complete-file run above also
passed after restoration. `git diff -- src/capabilities.py` is empty.

The earlier repository-wide formatting blocker is resolved using **Black
26.10.1**, the version pinned in `ci-python-versions.env`. Its official upstream
sources and dependencies were loaded into `/tmp`; no repository dependency was
changed. A temporary launcher dispatches every file through Black's normal
single-file checker because background worker reporting stalls in this sandbox.
Formatting and AST checks remain enabled. The changed test file was formatted
at line length 100, and the mandatory full command passed:

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
714 files would be left unchanged. (exit 0)
```

Delivery remains blocked. Staging the exact test file failed with
`Unable to create .git/index.lock: Read-only file system`; no commit or push
was possible. Automatic approval review rejected both the PR evidence comment
and the `needs-human` label with `MCP tool call requires approval, but approval
policy is never`. The source/test change and these results remain local, and
remote task checkboxes remain unchanged. A writable Git mount and an approved
GitHub mutation path are still needed to commit and publish the verified work.

## Post-cancellation and legacy heartbeat controls — 2026-11-17

Extended the named acceptance test without changing collection counts. Production
invocations after a renewal or live transition remain excluded until another
retirement. A heartbeat with no source metadata is retained beside explicitly
production invocations in both reports, with the same capability count, kill
switch and retirement evidence. Every report still preserves the ledger bytes.

Using Python 3.14.10 and official upstream pytest 9.0.2 in
`/tmp/issue-1414-test-tools`, with `ORCH_STATE_DIR` in `/tmp`:

- [x] Named test: `python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"` — **1 passed in 0.15s**.
- [x] Whole file: `python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"` — **25 passed in 0.25s**.
- [x] Gate-expiry and the four related firing-monitor regression files listed above — **79 passed in 2.46s**.
- [ ] Publish the deliberate-break/restoration evidence in the PR.
- [ ] Commit the verified test change on the checked-out branch.

The deliberate break inserted an unconditional `return None` immediately after
the helper docstring. The named test failed with **`assert 0 == 1`** at
`assert report["retired_but_invoked_count"] == 1` (**1 failed in 0.19s**, exit 1).
A `finally` block restored the original source bytes; the identical named command
then passed (**1 passed in 0.15s**, exit 0). Both complete-file regression commands
above ran after restoration. `git diff -- src/capabilities.py` is empty.

Black 26.10.1 formatted the changed test file at line length 100 and left it
unchanged. The required repository-wide formatting check passed (exit 0):

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
715 files would be left unchanged.
```

The temporary CLI launcher routes all discovered files through Black's normal
single-file checker because its background worker reporting stalls in this
sandbox. Formatting rules and AST-equivalence checks remain enabled; no
repository dependency or formatter configuration changed. `git diff --check`
also passed.

Staging failed with `Unable to create .git/index.lock: Read-only file system`.
Automatic approval review rejected the PR evidence comment, the `needs-human`
label, and the verified checkbox update: `MCP tool call requires approval, but
approval policy is never`. No commit was possible and remote checkboxes remain
unchanged. PR 1426 was verified open and ready (`draft=false`).

## Same-second heartbeat acceptance check — 2026-11-17

The named acceptance test now writes its production heartbeat at the retirement's
timestamp and adds a control whose production invocation precedes retirement at
that same timestamp. Both report readers must use append order to distinguish
them, preserving the exact evidence and kill switch. Collection counts are unchanged.

With Python 3.14.10 and pytest 9.0.2 from official tagged upstream sources in
`/tmp/issue-1414-test-tools`, all pytest commands select `-m "not slow"`:

- [x] Named test after exact source restoration: **1 passed in 0.25s**.
- [x] `python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"`:
  **25 passed in 0.22s**.
- [x] Gate-expiry and the four related firing-monitor regression files listed above:
  **79 passed in 2.51s**.
- [x] Deliberate break/restoration verified locally: unconditional `return None`
  caused **1 failed in 0.20s**, with **`assert 0 == 1`** at the retired count.
  Changing the timestamp comparison from `>=` to `>` also caused **1 failed in
  0.22s** at the same assertion, proving the added boundary case detects a regression.
  Each mutation was restored in a `finally` block and compared against the original
  bytes. The restored named command passed; `git diff -- src/capabilities.py` is empty.
- [ ] Publish this evidence in PR 1426 and commit the test change on the current branch.

Black 26.10.1 formatted the changed Python file at line length 100 and left it
unchanged. The required repository-wide command passed (exit 0):

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
715 files would be left unchanged.
```

The temporary launcher uses Black's ordinary single-file checker sequentially;
its background worker reporting stalled in this sandbox. All discovered files,
formatting rules, and AST-equivalence checks remain enabled. No repository
dependency or formatter configuration changed. `git diff --check` passed.

Staging the two changed files failed with `Unable to create .git/index.lock:
Read-only file system`, so no commit or push was possible. Automatic approval
review rejected adding `needs-human`, posting the evidence comment, and updating
the verified named-test/whole-file checkboxes: `MCP tool call requires approval,
but approval policy is never`. Remote checkboxes remain unchanged. PR 1426 was
verified open and ready (`draft=false`).

## Independent retired producers — 2026-11-17

Reviewed commits `f06f224` and `793380f` before extending the named acceptance
test. The implementation and earlier validation are present; the remote checklist
still has duplicate unchecked acceptance items. The new test case records a real
production heartbeat for a second retired capability and checks that both readers
report two distinct capabilities, each with its own invocation evidence and kill
switch. Multiple invocations of the first capability still count as one. Rendered
reports name both producers, and reads preserve the ledger bytes. Test collection
counts are unchanged.

Verified using Python 3.14.10, official upstream pytest 9.0.2 and Black 26.10.1
loaded into `/tmp/issue-1414-test-tools`, with `ORCH_STATE_DIR` in `/tmp` and
`-m "not slow"` on every pytest command:

- [x] Named acceptance test: **1 passed in 0.13s** after exact source restoration.
- [x] Whole firing-monitor file after restoration: **25 passed in 0.24s**.
- [x] Six-file targeted regression set listed above: **104 passed in 1.97s**.
- [x] Deliberate unconditional `return None`: **1 failed in 0.15s**, with
  **`assert 0 == 1`** at the retired capability count. A `finally` block restored
  the original source bytes; the named command then passed.
- [x] Black formatted the changed Python file at line length 100; unchanged.
- [x] `git diff --check` passed; `src/capabilities.py` has no diff.
- [ ] Publish the verified checklist reconciliation and new evidence on PR 1426.
- [ ] Commit the test change on the checked-out branch.

Automatic approval review rejected the checkbox update, evidence comment and
`needs-human` label with `MCP tool call requires approval, but approval policy is
never`. The reconciled body is saved at
`/tmp/issue-1414-validation/pr-body.md`; raw mutation/restoration outputs are in
the same directory. The Git metadata, index, objects and branch refs are
read-only. Remote checklist reconciliation and committing remain delivery
blockers rather than unverified implementation tasks.

The mandatory full formatting command passed (exit 0):

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
715 files would be left unchanged.
```

As in the earlier run, the temporary launcher dispatches CLI-discovered files
through Black's ordinary single-file checker sequentially because async worker
reporting stalls in this sandbox. Formatting and AST-equivalence checks remain
enabled. No repository dependency or formatter configuration changed.

After that gate passed, staging the two changed files failed with
`Unable to create .git/index.lock: Read-only file system`; the commit command
could not run. PR 1426 was re-read and remains open with `draft=false` at
`f06f2243efeb102b62af57ff3f99b01a595d4213`.

## Later fixture and independent clearing controls — 2026-11-17

Extended the named acceptance test on `codex/issue-1414` at `6b6120b`.
A fixture heartbeat appended after production activity cannot hide that activity
or enter either reader's invocation evidence. Reviving one of two anomalous
producers retains exactly the other producer's evidence and kill switch;
reviving both renders a measured zero. Every read preserves the ledger bytes.
This extends the existing test without changing collection counts.

Using Python 3.14.10 and official upstream pytest 9.0.2 loaded into
`/tmp/issue-1414-test-tools`, with `ORCH_STATE_DIR` in `/tmp`:

- [x] Named acceptance test: `python3 -m pytest tests/test_firing_monitor_live_rows.py::test_invocation_after_retirement_is_named -q -m "not slow"` — **1 passed in 0.15s**.
- [x] Whole file: `python3 -m pytest tests/test_firing_monitor_live_rows.py -q -m "not slow"` — **25 passed in 0.19s**.
- [x] Six-file focused regression set listed above, with `-m "not slow"`: **104 passed in 2.00s**.
- [x] Changed Python file formatted with CI-pinned Black 26.10.1, line length 100; unchanged.
- [x] `git diff --check` passed.

The already verified deliberate-break/restoration check above was not repeated.
Automatic approval review rejected the verified named-test/whole-file checkbox
update, evidence comment, and `needs-human` label, each with
`MCP tool call requires approval, but approval policy is never`. Remote
checkboxes remain unchanged. The reconciled body is retained at
`/tmp/issue-1414-validation/pr-body.md`. Git metadata is mounted read-only,
so committing remains blocked. No branch or pull request was created.

The mandatory full formatting command passed (exit 0):

```text
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
715 files would be left unchanged.
```

The temporary launcher dispatches CLI-discovered files through Black's ordinary
single-file checker sequentially; formatter options, file discovery, and
AST-equivalence checks remain enabled. No repository dependency or formatter
configuration changed.

After that gate passed, staging the exact test and verification files failed:
`Unable to create .git/index.lock: Read-only file system`. The conditional
commit command did not run. The verified test changes remain local.
