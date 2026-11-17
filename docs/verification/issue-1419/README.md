# Issue #1419: stuck lane target reporting and verification

## Implementation summary

`src/switch_review.py` implements `stuck_lane_targets(*, now=None, roots=None) -> dict`,
adds `"stuck_lane_targets": stuck_lane_targets(now=now)` to `review()`, and formats the
FYI section in `format_stuck_lane_targets(section)` and `format_report(rep)`.

Key behaviors:
- Reads the last 200 physical lines (`STUCK_JOURNAL_LINES = 200`) of
  `<state>/opener-core/rounds.jsonl` and `<state>/closer-core/rounds.jsonl` as bytes
  before decoding UTF-8, so invalid bytes in older history outside the 200-line window
  do not make a valid recent window unmeasured.
- Tracks each target's newest consecutive run of rounds with identical `action` and
  `reason` (or `error`) text, counting the final decision when a target appears more
  than once in a round and resetting the streak when the decision changes or the target
  is absent from a round.
- Flags a target as `SUSPECT` when its newest run is at least `STUCK_MIN_ROUNDS` (6)
  rounds long, spans at least `STUCK_MIN_SPAN_S` (3600 s), and is not a deliberate hold
  (`STUCK_HOLD_ACTIONS` or `STUCK_HOLD_REASONS`).
- Preserves missing, unreadable, timezone-naive, out-of-order, or malformed journals as
  `status: "unknown"` with a `measurement` string while retaining suspects measured in
  the readable lane, never conflating an unreadable journal with a drained lane.
- Exercises `stuck_lane_targets` and `format_stuck_lane_targets` in
  `switch_review._selftest_stuck_lane_targets()` as part of `src/switch_review.py --selftest`.

## Verification evidence

### 1. Named acceptance tests and suite execution

- `python3 -m pytest tests/test_switch_review_stuck_targets.py -q`:
  `50 passed in 0.32s` (including
  `test_repeated_identical_error_is_suspect`,
  `test_changing_reason_and_holds_are_not_suspect`,
  `test_fully_drained_prints_none_and_unreadable_prints_unknown`, and
  `test_selftest_stuck_lane_targets_exercises_drained_suspect_and_unknown`).
- `python3 -m pytest tests/test_switch_review*.py -q -m "not slow"`:
  `126 passed, 117 subtests passed in 1.23s`.
- `python3 src/switch_review.py --selftest`:
  `switch_review.py selftest: OK (... stuck_lane_targets flags repeated streaks while keeping drained and unreadable journals distinct ...)`.
- `node --test tests/test_switch_review_stuck_targets.js`: `5 passed`.
- `node --test tests/test_orchestrator_loop_svg.js`: `2 passed`.

### 2. Deliberate break -> revert under pytest

1. Set `STUCK_MIN_ROUNDS = 10**9` in `src/switch_review.py` and ran
   `python3 -m pytest tests/test_switch_review_stuck_targets.py::test_repeated_identical_error_is_suspect -q`:
   ```text
   FAILED tests/test_switch_review_stuck_targets.py::test_repeated_identical_error_is_suspect - assert 0 == 1
   1 failed in 0.23s
   ```
2. Reverted `STUCK_MIN_ROUNDS = 6` in `src/switch_review.py` and re-ran
   `python3 -m pytest tests/test_switch_review_stuck_targets.py -q`:
   ```text
   50 passed in 0.32s
   ```

### 3. Collection floor and formatting gate

- Independent full collection measurement (`python3 -m pytest --collect-only -q`):
  `9177 tests collected in 3.66s` (`origin/main` `38ed030` baseline `9161` + `16`
  branch cases). `.verify-floor.json` is updated to `collected: 9177`, `passed: 9175`
  with all skip, selftest, gate, and mypy ceilings preserved.
- Formatting and lint checks:
  `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .` passes.
