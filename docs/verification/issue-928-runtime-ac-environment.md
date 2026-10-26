# Runtime AC environment acceptance verification

Verified locally on 2026-10-26 on `codex/issue-928-runtime-ac-environment`.

- [x] Merged-worktree environment: the offline end-to-end test provisions an isolated
  venv, installs the checkout's package, and runs pytest and child interpreters while
  recording the selected interpreter. The expanded setup-failure regression covers
  nonzero exits, timeouts, and OS errors during both venv creation and installation.
  Each case verifies cleanup, no verification on failure, and provisioning on retry
  with the worktree interpreter recorded and passed to the runner.
  The installed-package regression also runs children via `sys.executable`,
  `python`, and `python3` outside the checkout with scheduler paths inherited;
  all three use the repository venv and import its installed package.
- [x] Spawned-interpreter classification: green merge CI plus the child-interpreter
  failure produces `UNRUNNABLE` / `environment_mismatch`; the sweep integration test
  verifies that it never reopens. Genuine assertion failures retain `FAIL`.
- [x] Reopen accounting: the already-open integration test verifies the state GET,
  absence of a reopen PATCH, `reopened=False`, `already_open=True`, and weekly count 0.
- [ ] Named regression passes on main: it passes on this branch; main verification
  remains pending delivery.
- [x] Deliberate-break guard: temporarily removed `or environment_mismatch(row)`
  from `delivery_failed()`'s early return. The named regression exited 1 with
  `AssertionError: 'FAIL' != 'UNRUNNABLE'`. Restored the source byte for byte and
  reran the same regression: 1 passed.

Validation used an isolated tooling venv populated from cached wheels, without
network access. With that venv's `bin` directory first on `PATH`, this command
reported **207 passed, 70 subtests passed**:

```bash
python -m pytest \
  tests/test_runtime_ac_post_merge_break.py \
  tests/test_runtime_ac_shadow.py \
  tests/test_durability_feedback_recovery.py \
  tests/test_durability_sweep_exact_merge.py \
  tests/test_durability_sweep_coverage_gaps.py \
  tests/test_durability_retry_horizon.py \
  tests/test_durability_stated_repair.py \
  tests/test_durability_fix_search_coverage.py -q -m 'not slow'
```

The first adjacent-suite run lacked the tooling venv on `PATH` and failed the
shadow-ingest subprocess test because bare `python3` had no pytest. The command
above fixes that validation environment and passes without production changes.

Black formatted the changed Python files, and their targeted checks pass. The
required repository-wide check subsequently passed with **714 files unchanged**
using `BLACK_NUM_WORKERS=1` and a writable `BLACK_CACHE_DIR` after the bounded
diagnostics populated the cache. Dependency verification
(`python3 scripts/sync_test_dependencies.py --verify`) and `git diff --check` pass.

Commit handoff is blocked by the runner's read-only `.git` mount: staging these
changes fails with `Unable to create '.git/index.lock': Read-only file system`.
The implementation and verification notes remain in the working tree.

PR #935 reconciliation: acceptance items 1, 2, 3, and 5 are verified locally;
item 4 remains pending its required main-branch run. The GitHub connector rejected
the PR-body update because it requires approval and this run's approval policy is
`never`, so remote checkboxes remain unchanged. The PR was open and ready for review
when inspected.

## Follow-up: scheduler pip install destinations

The recent commits were reviewed before this follow-up. Acceptance items 1, 2, 3,
and 5 remain verified; item 4 still requires delivery and verification on main.
The PR-body reconciliation was attempted again and rejected by the connector's
approval requirement, so the remote acceptance checkboxes remain unchecked.

Extended the existing offline provisioning regression with inherited `PIP_TARGET`,
`PIP_PREFIX`, and `PIP_USER`. Before the fix it failed with `UNRUNNABLE/tool_error`.
Provisioning now removes those install-location overrides while retaining index
and wheelhouse settings. The regression passes and confirms neither scheduler
destination was created. The mocked provisioning test verifies all three settings
are absent and the offline dependency-source settings are preserved.

The eight-suite command above still reports **207 passed, 70 subtests passed**.
Repeated the deliberate-break/revert demonstration: the named regression failed
with `FAIL != UNRUNNABLE`, then passed after restoring the source byte for byte.
Both changed Python files pass Black individually. A four-minute diagnostic using
a fork process pool exited 124 after reporting 714 files unchanged. The subsequent
required repository-wide command, using the populated writable cache, exited 0
and reported **714 files unchanged**:

```bash
BLACK_CACHE_DIR=/tmp/runtime-ac-black-cache BLACK_NUM_WORKERS=1 \
  black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .
```

Dependency verification and `git diff --check` pass.

Staging the two Python files was attempted again and failed with
`Unable to create '.git/index.lock': Read-only file system`. No commit or push was
possible in this run.
