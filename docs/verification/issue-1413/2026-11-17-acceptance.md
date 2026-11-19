# PR #1421 acceptance verification

Checked on 2026-11-17 from `orch/issue-1413`, starting at `f5a9ba0`.

The named regression now supplies measured downstream zeros and verifies that the
unknown usable step still leaves `first_break=None`. It checks the readable
unmeasured reason, rejects `usable 0` and `usable None`, and verifies reporting
does not modify the ledger. The same invocation/output-only window is then read
with an explicit `usable_signal="success"` declaration: usable becomes zero and
the first break becomes `unusable`.

## Verified acceptance

- [x] Named regression passes, including `first_break=None` and rendered unmeasured.
- [ ] Deliberate break/revert evidence published to the PR. Local evidence is below;
  publishing is blocked by the GitHub tool's approval requirement.
- [x] The entire test file passes: 35 passed.
- [x] Existing tests retained deliberately. The positive-success fixture
  `test_counts_exclude_fixture_trials_and_ungraded_edges` is unchanged. The new
  measured-zero control explicitly declares `usable_signal="success"`.

## Deliberate break and restoration

Temporarily replaced the generic measurement expression in
`src/value_chain_monitor.py`:

```python
usable = success_count if cap.get("usable_signal") == "success" or success_count else None
```

with unconditional counting:

```python
usable = success_count
```

Ran:

```sh
python3 -m pytest tests/test_value_chain_monitor.py::test_generic_row_without_success_signal_reports_usable_unmeasured -q -m "not slow"
```

Observed exit 1:

```text
>       assert row["usable"] is None
E       assert 0 is None

tests/test_value_chain_monitor.py:291: AssertionError
1 failed in 0.15s
```

Restored the original source bytes in a `finally` block and verified byte equality.
The restored source SHA-256 is
`4b3c9c628118249f410e1d89a40f1bc22b1a0419a41a01e1c43b18bd85636956`.
The same command then exited 0: `1 passed in 0.10s`.

The complete file command also exited 0:

```sh
python3 -m pytest tests/test_value_chain_monitor.py -q -m "not slow"
```

```text
35 passed in 4.79s
```

## Environment and handoff

Validation used CPython 3.14.10 and pytest 8.4.2. The default interpreter had no
pytest or Black, and PyPI DNS was unavailable. Temporary tools were assembled
under `/tmp/orchestrator-1421-tools` from official tagged GitHub sources using the
connected GitHub read tools. All 154 downloaded source files were checked against
their Git blob hashes; generated version modules supplied build metadata. System
Click, packaging and Pygments came from `/usr/lib/python3/dist-packages`.
Commands used:

```sh
export PYTHONPATH=/tmp/orchestrator-1421-tools:/usr/lib/python3/dist-packages
```

The formatting gate used the repository-pinned Black 26.10.1 (declared in both
`.github/workflows/autofix-versions.env` and `ci-python-versions.env`) on CPython
3.13.18. Its official tagged sources and updated pathspec 1.0.0 / pytokens 0.4.1
dependencies were installed under `/tmp/orchestrator-1421-black26`; all 85 source
files were verified against their Git blob hashes. The earlier Black 25.9.0
diagnostic was stopped after reporting differences in unrelated files; those
files were not edited.

The sandbox denies local socket sends, including `socket.socketpair().send()`.
This prevents Black's normal event loop from receiving worker-completion
notifications. A temporary launcher under `/tmp` supplied a 50 ms event-loop
timer and `BLACK_NUM_WORKERS=1`; Black's source and formatting checks were
unchanged. The shell's `black` function invoked that launcher with the original
arguments and the pinned source directory first in `PYTHONPATH`.

Both required formatting commands exited 0:

```sh
black --line-length 100 tests/test_value_chain_monitor.py
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

```text
1 file left unchanged.
712 files would be left unchanged.
```

`git diff --check` also passed. No production source changes from the deliberate
break remain.

The requested commit is blocked: `git add -- tests/test_value_chain_monitor.py`
exits 128 because `.git/index.lock` is on a read-only filesystem. The source/test
changes must be committed from a writable checkout.

Both the PR evidence comment and the additive `needs-human` label were attempted.
Each GitHub tool returned `MCP tool call requires approval, but approval policy
is never`; neither mutation occurred. PR #1421 was last observed open and ready
for review (`draft=false`) at `f5a9ba0`. Publishing this evidence and updating the
PR checkboxes require an authorized run.

## Revalidation from `f5f91ed` (2026-11-17)

Reviewed the six commits since `263b8ef`: the implementation, eligibility and
window-boundary controls, strengthened named regression, and isolation of live
weekly-review probes are already present. No existing test was deleted; the
positive-success fixture remains unchanged, and the measured-zero control
explicitly declares `usable_signal="success"`.

Strengthened the named regression further to verify that declaring the success
signal preserves situation, offered, invocation, acted-on and outcome counts.
It also checks that reporting the declared ledger leaves its bytes unchanged.

Using pytest 8.4.2 with `-m "not slow"`:

- Named regression before changes: `1 passed in 0.85s`.
- Full file after the new assertions: `35 passed in 6.44s`.
- Deliberate unconditional `usable = success_count`: exit 1,
  `assert 0 is None` at `tests/test_value_chain_monitor.py:296`,
  `1 failed in 0.19s`.
- Exact source-byte restoration, then the same named command: exit 0,
  `1 passed in 0.13s`. The restored source SHA-256 matches the value above.

The PR body already describes the earlier deliberate break and restoration.
Reconciliation of its unchecked named-test acceptance item was attempted after
the current passing run. The GitHub update returned
`MCP tool call requires approval, but approval policy is never`. Publishing the
current evidence comment and adding `needs-human` each returned the same error;
none of those remote changes occurred.

Staging the test change was attempted with
`git add -- tests/test_value_chain_monitor.py`; it exited 128 because
`.git/index.lock` is on a read-only filesystem. No commit was created.

The final formatting gate passed with Black 26.10.1 on CPython 3.13.18:

```text
black --line-length 100 tests/test_value_chain_monitor.py
1 file left unchanged.
black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
712 files would be left unchanged.
```

As above, the temporary launcher added an event-loop wake timer to accommodate
the sandbox's socket restrictions. The first full check was interrupted by its
55-second timeout; the completed run above exited 0. `git diff --check` passed.
PR #1421 was re-read after the failed remote mutations: it remains open and ready
for review (`draft=false`) at `f5f91ed`.
