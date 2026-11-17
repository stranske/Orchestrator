# Issue #1419: local implementation and tooling blocker

Prepared on `codex/issue-1419` for PR #1425. Changes remain uncommitted because
the task explicitly forbids committing Python changes until Black passes.
No acceptance checkbox has been marked complete.

The detector and weekly report integration are implemented in `src/switch_review.py`.
Fifteen regression cases cover repeated errors, changing decisions, deliberate holds,
threshold boundaries, target disappearance, duplicate items, future timestamps,
bounded journal history, corrupt/missing journals, independent lane measurements,
default state paths, and the five-row text limit. The test module is registered in
the shard manifest. Architecture prose and diagram describe the same FYI surface.

Validation performed:

- All 15 new test functions passed when invoked directly with separate temporary
  journal directories. This does not claim a pytest run or full-suite collection.
- `python src/switch_review.py --selftest` passed.
- Deliberately changing `STUCK_MIN_ROUNDS` from `6` to `10**9` made the direct
  invocation of `test_repeated_identical_error_is_suspect` fail with `AssertionError`
  at `assert report["suspect"] == 1`. Restoring `6` made it pass. The source is restored.
- Python AST parsing, SVG XML parsing, unique shard registration and
  `git diff --check` passed.

The environment has neither pytest nor Black. Attempts to install them failed;
PyPI name resolution was unavailable. The named pytest command and collection
command fail with `No module named pytest`; Black commands fail with
`black: command not found`. No commit was attempted after that failed gate.

The collection floor is an expectation of 9,115 collected / 9,113 passed, derived
from the baseline plus the 15 added test functions. Full collection must confirm
these numbers; all existing skip and other ceilings are preserved.

Required follow-up in an environment with pytest and Black:

1. Run `python -m pytest tests/test_switch_review_stuck_targets.py -q -m "not slow"`.
2. Run the existing switch-review tests with `-m "not slow"`.
3. Confirm full collection and reconcile `.verify-floor.json` against the observed count.
4. Repeat the deliberate-break demonstration under pytest and record it in the PR.
5. Run `black --line-length 100 src/switch_review.py tests/test_switch_review_stuck_targets.py`.
6. Run `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`.
7. Commit the prepared changes on the existing branch once the required checks pass.
8. Reconcile task checkboxes from verified evidence and keep PR #1425 ready for review.

Attempts to add a blocker comment and `needs-human` label to PR #1425 were both
rejected by the connector: `MCP tool call requires approval, but approval policy
is never`. Neither update was applied.
