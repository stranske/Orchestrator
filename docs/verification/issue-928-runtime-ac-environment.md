# Runtime AC environment acceptance verification

Verified locally on 2026-10-26 on `codex/issue-928-runtime-ac-environment`.

- [x] Merged-worktree environment: the offline end-to-end test provisions an isolated
  venv, installs the checkout's package, and runs pytest and child interpreters while
  recording the selected interpreter. The expanded setup-failure regression covers
  nonzero exits, timeouts, and OS errors during both venv creation and installation.
  Each case verifies cleanup, no verification on failure, and provisioning on retry
  with the worktree interpreter recorded and passed to the runner.
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
reported **207 passed, 67 subtests passed**:

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

Black formatted the changed Python file, and its targeted `--check --line-length 100`
passes. The required repository-wide check has not returned a result and is not
claimed as passing. It uses `BLACK_NUM_WORKERS=1` because the default multiprocessing
startup was denied by the sandbox. `git diff --check` passes.

Commit handoff is blocked by the runner's read-only `.git` mount: staging these
changes fails with `Unable to create '.git/index.lock': Read-only file system`.
The implementation and verification notes remain in the working tree.
