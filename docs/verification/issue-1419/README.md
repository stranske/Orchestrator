# Issue #1419: local implementation and tooling blocker

## Follow-up: decode only the retained journal window

The journal reader now selects the last 200 physical lines as bytes before UTF-8
decoding. Previously, invalid UTF-8 in older, excluded history made a valid recent
window report unknown. The existing bounded-history regression now exercises both
invalid JSON and invalid UTF-8 outside that window and checks the retained start
timestamp. Invalid UTF-8 inside the window still reports unknown in the existing
corrupt-journal test. No test functions were added, so the collection floor is unchanged.

The extended regression failed before the source fix and passed afterward. All 15
regression functions passed through direct invocation with separate temporary
directories; `python src/switch_review.py --selftest` and `git diff --check` also
passed. These direct invocations do not establish a pytest result.

The named pytest command still fails with `No module named pytest`. Black formatting
and the required repository-wide Black check both fail with `black: command not
found`. Installing pytest and Black into a temporary directory failed because pip
could not obtain either distribution. The Python changes from this follow-up remain
uncommitted under the required formatting gate. Run the pytest and Black commands
below in a provisioned environment before committing on this same branch. No
acceptance checkboxes were changed in this follow-up; the live issue and PR summary
already had all boxes checked when inspected.

Posting this follow-up as a PR comment and adding `needs-human` were both rejected
with `MCP tool call requires approval, but approval policy is never`; neither
mutation was applied. PR #1425 was rechecked as open and ready for review.

## Original implementation record

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
