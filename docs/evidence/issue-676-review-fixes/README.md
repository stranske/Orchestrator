# PR #686 review-fix evidence

- `focused-green.txt`: requested focused pytest command on the working tree.
- `break-phase-one-duals.txt` / `restore-phase-two-duals.txt`: the 300-case property test fails when final-tableau duals are replaced by the actual Phase-I tableau duals, then passes after restoration.
- `break-pools-only.txt` / `restore-agents-shape.txt`: the agent-shape fixture fails when agent pacing is ignored, then passes after restoration.
- `mutations-restored-green.txt`: all 64 focused tests pass after both deliberate breaks are reverted byte for byte. Mutations ran on isolated source/test copies so the concurrent full verification never saw broken source.
- `verify.txt`: interrupted initial `python3 src/verify.py` run (exit 130), superseded because both temporary-directory overrides were not explicitly set. Its built-in mechanism still used a read-only Brain backup/private state. Cancellation through the owned execution session succeeded after ordinary process-group signals were denied. No verdict is claimed for that initial run.
- `verify-reconcile-floor.txt`: final `python3 src/verify.py --reconcile-floor` run, with both `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` in temporary directories. Live capability state and a read-only SQLite backup seed the private runtime; the verifier then makes its own private copy. Only the verifier may raise the floor; no floor or ceiling is manually weakened.
- `main-failures.txt`: all 21 exact failing test IDs from the final branch verifier also fail on clean origin/main, with isolated state/runtime and copied inputs.
- `watchdog-main.txt`: the failing watchdog selftest also fails on clean origin/main.
- `muse-branch.txt` / `muse-main.txt`: the named Muse keychain regression passes on the branch and on a clean detached origin/main checkout.
- `live-report.txt`: requested 14-day CLI observation on a temporary Brain backup, with temporary state/runtime paths. `live-report-isolation.txt` records the source history paths and isolation. Missing history is unmeasured, not zero differing classes.

The LP's accepted constraints are all nonnegative-RHS inequalities. Phase I has an explicit feasible slack basis and zero auxiliary objective, so no artificial pivots are necessary. Economic duals are the Phase-II slack reduced costs. The assignment is a quantity map (including fractional splits); unmet demand is explicit. Score ties use the optimal allocation, then agent name.

Final full verdict: 3998 passed, 21 failed, 4019 collected; tick_watchdog selftest failed. The verifier refused the floor update. Main's collected/passed floor of 3955 and all ceilings remain unchanged. A green full run is still required before a legitimate floor reconciliation.
