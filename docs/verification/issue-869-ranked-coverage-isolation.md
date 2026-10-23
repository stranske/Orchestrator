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

Commit blocker: local `git add` cannot create `.git/index.lock` because `.git` is read-only. GitHub connector `update_file` also refused the commit: "MCP tool call requires approval, but approval policy is never". The patch remains local and needs a commit-capable follow-up run. Historical #853 combined-CI provenance and evidence limitations remain unchanged; no new uncovered-unit gain is claimed.

Attempts to update the PR body, add this comment, and apply `needs-human` were also rejected by the same connector approval policy. No remote mutation succeeded. Existing historical combined coverage remains linked from the PR to [CI run 39545834989](https://github.com/stranske/Orchestrator/actions/runs/39545834989); it was not rerun here.

