# Issue #869: ranked coverage isolation follow-up

Run date: 2026-10-22/23 UTC. Base: `0a82d7a685d2d355c5dfcb1c543543d99a209247`.

Reconciled recent commits through `0a82d7a` against the PR tasks: all eight checkboxes are already checked; the pre-change standalone isolation module passes (9 tests).

Additional scoped fix prepared in the working tree: block DNS resolution before socket connections and add ten safe-spy regressions proving process, gh, agent, connection, and resolver attempts are denied and counted even when their exceptions are caught. No real external boundary is invoked by those regressions.

Validation using cached local pytest/Black wheels in `/tmp/orch872-py314`:
- `python -m pytest -q tests/test_issue853_ranked_coverage.py -m "not slow"`: 19 passed, with the module's own isolation and no runtime wrapper. This includes the existing safe database break/restore regression.
- Before the resolver fix, `python -m pytest -q tests/test_issue853_ranked_coverage.py -k external_boundary -m "not slow" --tb=short`: 4 failed, 6 passed, 9 deselected. Failures reached harmless resolver spies, never DNS.
- Unwrapped neighbors (`tests/test_research_control.py tests/test_research_intake.py tests/test_research_ownership.py tests/test_research_usage_guard.py -m "not slow"`): 1 failed, 59 passed, 99 subtests passed. Existing `test_the_lane_accepts_research_items` attempted default feedback-directory creation; the read-only sandbox blocked creation. That separate test was not changed.
- Repeating those neighbors with `ORCH_STATE_DIR=/tmp/orch872-neighbors/state ORCH_LOCAL_RUNTIME=/tmp/orch872-neighbors/runtime ORCH_FEEDBACK_DB=/tmp/orch872-neighbors/runtime/feedback/orchestrator.db`: 60 passed, 99 subtests passed.
- `BLACK_NUM_WORKERS=1 BLACK_CACHE_DIR=/tmp/orch872-black-cache black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`: passed, 645 files unchanged. A temporary event-loop polling workaround collected the initial formatting results because the sandbox blocks worker socket wakeups; the ordinary required command then passed using that verified cache.
- `git diff --check`: passed.

Commit blocker during that run: local `git add` could not create `.git/index.lock` because `.git` was read-only. GitHub connector `update_file` also refused the commit: "MCP tool call requires approval, but approval policy is never". The automation subsequently committed the patch and this evidence in `b627a6f`. Historical #853 combined-CI provenance and evidence limitations remain unchanged; no new uncovered-unit gain is claimed.

Attempts to update the PR body, add this comment, and apply `needs-human` were also rejected by the same connector approval policy. No remote mutation succeeded. Existing historical combined coverage remains linked from the PR to [CI run 39545834989](https://github.com/stranske/Orchestrator/actions/runs/39545834989); it was not rerun here.

## Path-escape regression follow-up (2026-10-23)

Reviewed the six recent implementation commits through `b627a6f` and the live body of PR #872:
all eight checkboxes were already checked, and the PR was open with `draft=false`.
The standalone module passed 19 tests before this follow-up.

Extended the real SQLite-boundary regression to exercise parent traversal and symlink escapes
through both direct `sqlite3.connect` and the original feedback connection factory. All sentinel
paths stay under pytest's temporary directory; a driver spy prevents any real SQLite open during
these containment regressions.

Using `/tmp/orch872-venv/bin/python` (pytest/Black restored from cached local wheels):

- `python -m pytest -q tests/test_issue853_ranked_coverage.py -m "not slow"`: 23 passed.
- Safe mutation: temporarily removed `.resolve()` from the containment helper, ran the module
  with `-k sqlite_boundary -m "not slow" --tb=short`, and observed 4 failed, 2 passed,
  17 deselected. The four new cases reached only the driver spy. Restored the helper in a
  `finally` block and repeated the standalone command: 23 passed.
- `ORCH_STATE_DIR=/tmp/orch872-neighbors/state ORCH_LOCAL_RUNTIME=/tmp/orch872-neighbors/runtime
  ORCH_FEEDBACK_DB=/tmp/orch872-neighbors/runtime/feedback/orchestrator.db python -m pytest -q
  tests/test_research_control.py tests/test_research_intake.py tests/test_research_ownership.py
  tests/test_research_usage_guard.py -m "not slow"`: 60 passed, 99 subtests passed.
- Targeted coverage reporting with `--cov=src/research_scheduler --cov-report=term-missing`
  could not run because pytest-cov is unavailable (unrecognized arguments). No new coverage
  percentage or uncovered-unit gain is claimed; the historical CI provenance above is preserved.
- `git diff --check`: passed.

Local staging remains blocked: `git add -- tests/test_issue853_ranked_coverage.py` cannot create
`.git/index.lock` on the read-only Git metadata filesystem.

Formatting passed: `BLACK_NUM_WORKERS=1 BLACK_CACHE_DIR=/tmp/orch872-black-cache black --check
--line-length 100 --exclude '(\.workflows-lib|node_modules)' .` reported 645 files unchanged.
Before that ordinary command, Black's single-file runner checked the same discovered files
sequentially and populated its verified cache because the sandbox stalls worker notifications.
The changed test file was also run through `black --line-length 100`.

The GitHub connector rejected the source-file commit, PR-body reconciliation update, blocker
comment, and `needs-human` label with "MCP tool call requires approval, but approval policy is
never". No remote mutation succeeded during that run. The automation subsequently committed
the test follow-up and this evidence in `ca518a2`.

## Cached-path rebinding regression (2026-10-23)

Reviewed the recent commits through `ca518a2` and reconciled the live PR body: all eight task
and acceptance checkboxes were already checked. PR #872 was open with `draft=false`.

Added `test_runtime_fixture_rebinds_preexisting_environment_and_cached_database` to verify
that the module fixture replaces preexisting `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`,
`ORCH_FEEDBACK_DB`, and the already-imported `feedback.DB_PATH`. It checks all four bindings
before invoking the real connection factory, verifies SQLite's actual database path, and
checks that fixture teardown restores the prior bindings without creating the stale root.
Every path in this regression is under pytest's temporary directory.

Using `/tmp/orch872-venv/bin/python` with pytest/Black restored from cached local wheels:

- Baseline standalone module: 23 passed.
- `python -m pytest -q tests/test_issue853_ranked_coverage.py -m "not slow"`: 24 passed.
- Safe mutations: removed each of the four fixture bindings individually, then ran
  `python -m pytest -q tests/test_issue853_ranked_coverage.py -k
  runtime_fixture_rebinds_preexisting_environment_and_cached_database -m "not slow" --tb=short`.
  Each mutation produced 1 failed, 23 deselected at the corresponding binding assertion,
  before the connection factory. Restored the source in a `finally` block and reran the
  standalone module: 24 passed.
- Neighbors with `ORCH_STATE_DIR=/tmp/orch872-neighbors/state`,
  `ORCH_LOCAL_RUNTIME=/tmp/orch872-neighbors/runtime`, and
  `ORCH_FEEDBACK_DB=/tmp/orch872-neighbors/runtime/feedback/orchestrator.db`:
  `python -m pytest -q tests/test_research_control.py tests/test_research_intake.py
  tests/test_research_ownership.py tests/test_research_usage_guard.py -m "not slow"`:
  60 passed, 99 subtests passed. Only the neighbors use the runtime wrapper; the isolation
  module runs with its own fixture.

Historical #853 combined-CI provenance and partial-round limitations remain as recorded above;
this follow-up claims no additional uncovered-unit gain.

Formatting and publication results for this follow-up:

- `black --line-length 100 tests/test_issue853_ranked_coverage.py`: passed, unchanged.
- `BLACK_NUM_WORKERS=1 BLACK_CACHE_DIR=/tmp/orch872-black-cache black --check --line-length 100
  --exclude '(\.workflows-lib|node_modules)' .`: passed, 645 files unchanged. The initial
  process-pool runs stalled, so Black's CLI source discovery and single-file checker were run
  sequentially with the same settings to populate the verified cache; the ordinary command
  above then exited 0.
- `git diff --check`: passed.
- `git add -- tests/test_issue853_ranked_coverage.py
  docs/verification/issue-869-ranked-coverage-isolation.md`: failed because `.git/index.lock`
  cannot be created on the read-only Git filesystem. No local commit was possible.
- GitHub source-file commit, PR-body update, blocker comment, and `needs-human` label were all
  rejected with "MCP tool call requires approval, but approval policy is never". This follow-up
  remains in the working tree for a commit-capable run; no remote mutation succeeded.

## Remaining guarded-boundary regressions (2026-10-23)

At base `88dae7b`, all eight checkboxes were checked and PR #872 was open with
`draft=false`. The baseline standalone module passed 24 tests.

Extended the existing deny-and-count regression to cover `subprocess.call`, `check_call`,
`check_output`, and the socket methods `connect`, `connect_ex`, and `sendto`. A dummy
receiver avoids constructing sockets, and each boundary has a harmless fallback spy.

Using `/tmp/orch872-venv/bin/python` with dependencies restored from cached wheels:

- `python -m pytest -q tests/test_issue853_ranked_coverage.py -m "not slow"`: 30 passed,
  using only the module's own runtime isolation.
- Removed each of the six corresponding guards individually and ran its exact parametrized
  regression with `-m "not slow" --tb=short`: each failed, reaching only its fallback spy.
  Restored the source in a `finally` block; the standalone module again passed all 30 tests.
- `python -m pytest -q tests/test_research_control.py tests/test_research_intake.py
  tests/test_research_ownership.py tests/test_research_usage_guard.py -m "not slow"`:
  60 passed, 99 subtests passed. Only these neighbors used a wrapper setting `ORCH_STATE_DIR`,
  `ORCH_LOCAL_RUNTIME`, and `ORCH_FEEDBACK_DB` under `/tmp/orch872-round-neighbors`.
- Targeted `--cov=src/research_scheduler --cov-report=term-missing` reporting could not run:
  pytest-cov is unavailable. No new coverage percentage or uncovered-unit gain is claimed;
  the historical combined-CI evidence and limitations above remain unchanged.
- `black --line-length 100 tests/test_issue853_ranked_coverage.py`: passed, unchanged.
- `BLACK_NUM_WORKERS=1 BLACK_CACHE_DIR=/tmp/orch872-black-cache black --check
  --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`: passed, 645 files unchanged.
  The initial process-pool check timed out; Black's single-file checker populated its verified
  cache before the successful ordinary CLI check.
- `git diff --check`: passed. Local staging failed because `.git/index.lock` cannot be created
  on the read-only Git filesystem. The GitHub source-file commit was also rejected:
  "MCP tool call requires approval, but approval policy is never". Changes remain uncommitted.
  The blocker comment and `needs-human` label were rejected by the same approval policy;
  no remote mutation succeeded.
