# Issue #1419 verification

[PR #1425](https://github.com/stranske/Orchestrator/pull/1425) delivered the weekly
journal report. Its [merge-commit CI](https://github.com/stranske/Orchestrator/actions/runs/44062449785)
reported 9,176 collected tests against a floor of 9,176.

On 2026-11-17, the repair verified that six rounds spaced 720 seconds apart report
both `skip-held` with reason `experiment lock busy` and `error` with a `held:`
reason. The `already-parked` and `complete` decisions and `skip` or `skip-held`
with a `held:` reason are exempt.

| Command | Result |
| --- | --- |
| `/opt/anaconda3/bin/python3 -m pytest tests/test_switch_review_stuck_targets.py -q` with the repaired predicate | `49 passed in 4.51s` |
| `/opt/anaconda3/bin/python3 -m pytest tests/test_switch_review_stuck_targets.py::test_repeated_identical_error_is_suspect -q` with `STUCK_MIN_ROUNDS = 10**9` | `assert report["suspect"] == 1` failed with `assert 0 == 1` (`1 failed in 0.33s`) |
| Same named test after restoring `STUCK_MIN_ROUNDS = 6` | `1 passed in 0.29s` |
| Focused module after restoration | `49 passed in 4.50s` |
