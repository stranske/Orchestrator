# PR #686 review-fix evidence (historical round 1)

The round-1 claim that these sandbox failures were pre-existing main defects is
withdrawn. The re-reviewer could not reproduce them on clean main, and the prior
head's hosted checkout CI check succeeded. The sandbox comparison captures below
remain historical evidence only. Current validation and the corrected floor
are recorded in `../issue-676-round2/`.

- `focused-green.txt`: requested focused pytest command on the working tree.
- `break-phase-one-duals.txt` / `restore-phase-two-duals.txt`: the 300-case property test fails when final-tableau duals are replaced by the actual Phase-I tableau duals, then passes after restoration.
- `break-pools-only.txt` / `restore-agents-shape.txt`: the agent-shape fixture fails when agent pacing is ignored, then passes after restoration.
- `mutations-restored-green.txt`: all 64 focused tests pass after both deliberate breaks are reverted byte for byte. Mutations ran on isolated source/test copies so the concurrent full verification never saw broken source.
- `verify.txt`: interrupted initial `python3 src/verify.py` run (exit 130), superseded because both temporary-directory overrides were not explicitly set. Its built-in mechanism still used a read-only Brain backup/private state. Cancellation through the owned execution session succeeded after ordinary process-group signals were denied. No verdict is claimed for that initial run.
- `verify-reconcile-floor.txt`: final `python3 src/verify.py --reconcile-floor` run, with both `ORCH_STATE_DIR` and `ORCH_LOCAL_RUNTIME` in temporary directories. Live capability state and a read-only SQLite backup seed the private runtime; the verifier then makes its own private copy. That run did not reconcile the floor. Round 2 follows the owner's explicit instruction to set the collected/passed expectation to the merged collection count while preserving all ceilings.
- `main-failures.txt`: historical replay of the 21 failing test IDs in the same restricted sandbox. This does not establish a pre-existing repository defect.
- `watchdog-main.txt`: historical watchdog replay in the same restricted sandbox, subject to the same limitation.
- `muse-branch.txt` / `muse-main.txt`: the named Muse keychain regression passes on the branch and on a clean detached origin/main checkout.
- `live-report.txt`: requested 14-day CLI observation on a temporary Brain backup, with temporary state/runtime paths. `live-report-isolation.txt` records the source history paths and isolation. Missing history is unmeasured, not zero differing classes.

The LP's accepted constraints are all nonnegative-RHS inequalities. Phase I has an explicit feasible slack basis and zero auxiliary objective, so no artificial pivots are necessary. Economic duals are the Phase-II slack reduced costs. The assignment is a quantity map (including fractional splits); unmet demand is explicit. Score ties use the optimal allocation, then agent name.

Historical round-1 full verdict: 3998 passed, 21 failed, 4019 collected; tick_watchdog selftest failed. The verifier refused the floor update. Main's collected/passed floor of 3955 and all ceilings remain unchanged. These are historical counts, superseded by the owner-requested round-2 measured floor; they do not claim pre-existing main failures.
