# PR #686 review fixes, round 2

Main merged: `8c53ae1ae3cec9c67a2fa12b3ad56cec14d1dec5` (#680).
The conflict resolution preserves capacity/pacing export fields, main's capacity
semantic comparison, and comparative rank/rounded-lambda/provenance compare keys.
The exporter builds one fresh capacity snapshot shared by both additive blocks.
Both `_dense_simplex` and `shadow_prices` remain byte-identical to `d881907`.

- `focused-green.txt`: `python3 -m pytest tests/test_comparative.py tests/test_route_weights_export*.py tests/test_pacing.py -q`; 246 passed.
- `collect.txt`: final merged tree collects 4128 tests = main's 4053 +75 comparative cases (11 net new cases in round 2). Both floor counts are 4128 expectations; all main ceilings and main's note are preserved.
- `resets-gate-red.txt`: reinstate the numeric `resets_available` gate in an isolated module copy; the fixture built by real `pacing.evaluate(..., resets=None)` fails (`inputs_unmeasured` versus `optimal`).
- `resets-gate-restored-green.txt`: restore the isolated module byte for byte; the same test passes. Matching SHA-256 values prove restoration. The worktree/full verifier never saw the deliberately broken module.
- `live-report.txt` / `live-report-stderr.txt`: `python3 src/route_weights_export.py --comparative-report --window-days 14` with temporary runtime/state and a read-only SQLite backup. `isolation.txt` records paths and missing history sources. Neither capacity-ledger.ndjson nor pacing-history.ndjson exists in the live state; the result remains unmeasured, not zero differing classes.
- `prior-head-ci-checks.txt`: REST readback for the prior head `d881907`. Hosted checkout CI succeeded (3993 passed, zero failed, 26 skipped; collection-floor lag allowed by that job). The flat exec-mirror job failed on `test_shipped_config_names_argon_and_parses` (`assert 0 > 0`) and the stale floor. This is prior-head evidence, not current-head acceptance.

Round-1 claims that the restricted sandbox's failures were pre-existing main
repository defects are withdrawn. The clean-worktree re-review did not reproduce
them. Round-1 replay captures remain historical evidence only. Hosted CI judges
the pushed round-2 head; this run does not wait for it.

`watchdog-selftest.txt` separately captures the full local selftest exception
(`PermissionError: [Errno 1] Operation not permitted: 'ps'`), because the verifier
truncates selftest exception text in its summary. No permission changes were made.

Full verifier on the requested `8c53ae1` merge: 4105 passed, 23 failed, zero
skipped, 4128 collected; 104 of 106 selftests passed, with `adapters` and
`tick_watchdog` failing. All capability gates passed. `verify.txt` captures
the summary, `pytest-failures.txt` lists every exact JUnit failure message, and
`pytest-failures.json` retains their full traces. `adapters-selftest.txt`
captures the temporary-home containment assertion in full. The exporter shed
marker test passed focused but failed in the full sandbox; current-head CI
confirmation remains pending. No pre-existing-main attribution is made.
