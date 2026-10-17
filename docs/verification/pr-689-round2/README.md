# PR #689: review fixes, round 2

Local runtime: `/opt/anaconda3/bin/python3`. Initial PR head: `0cea23866077b944a10791c3a0052adda0782d59`.
Initial main: `afe56a91326f3e3360f99552e437023e3d217261` (already an ancestor; no conflicts).
A later pre-push sync merged `f6a133ee3078624dded5124727b643ef58668072` cleanly;
its four monitor regression tests pass.

The changes make terminal trial verdicts immutable, wait through the seven-day merge grace,
keep unknown merge states pending with daily polling, and keep existing producer followup
independent of the launch switch. Healthy arms remain selectable after coverage saturation.
Unnamed-test rejections count once per issue per UTC day, the last plan skip is not counted
twice, the no-PR lock bound has one constant, and shell default failures are visible.
Node-scoped local-verifier selftests and copy-scope reporting now use the containing file.

## Validation

- Collection: **4,246** tests; the floor matches this independently measured population.
  `passed` is an expectation, not an observed full-suite pass count. Every ceiling is unchanged.
- Direct merged-worktree producer and node-scope regression run: **62 passed**
  (60 producer cases and two copy-scope cases), exit 0.
- Requested broader suite: **323 passed, 11 failed, 131 subtests passed**.
  Command: `ORCH_FEEDBACK_DB=/tmp/689-focused-feedback.db /opt/anaconda3/bin/python3 -m pytest tests/test_experiment_producer.py tests/test_tick*.py tests/test_research*.py tests/test_synthesis*.py tests/test_exp_abcd*.py tests/test_switch_review*.py tests/test_capacity*.py tests/test_local_verify*.py -q`.
  Ten failures require process access (`ps`) unavailable in this sandbox. The remaining
  ABCD selftest tries to unlink a Vibe runtime config outside the writable roots.
  The temporary feedback database avoids an existing intake test's production-database write.
- Standalone `src/local_verify.py --selftest`: **PASS**, exit 0.
- Ruff, Black and MyPy on the changed modules: **PASS**. SVG XML parse: **PASS**.
- Full verifier on the merged tree: **RED**, exit 1: **4,215 passed, 31 failed,
  314 subtests passed**, and three failed module selftests (`capability_propensity`,
  `exp_abcd`, `tick_watchdog`). The full failure list is retained in `full-verify.log`.
  The entrypoint diagnostic was not checked because `capabilities.json.lock` is outside
  the sandbox's writable roots. Denied process and runtime-config access are observed in the broader-suite receipt;
  the complete full-gate failure population was not
  independently triaged against a base-branch run. A green full gate remains required
  for acceptance. Collection is 4,246 = 4,215 passing + 31 failing tests; the failed
  passed-floor check is not evidence of a collection drop.

The three requested mutations are defined in `prove_breaks.py`; each failed with exit 1,
then passed with exit 0 after byte-for-byte source restoration. The isolated proof sources
were compared byte-for-byte against the merged worktree, then all three mutations were
repeated directly in the worktree after the full verifier completed. Separate RED/GREEN logs retain
the console captures and restored source SHA-256. All new agent, GitHub, and delivery test
boundaries are mocked; the tests do not launch agents, push branches, or contact GitHub.

No deployment or live-trial outcome is established by these tests. No permission settings,
merge gates, skip ceilings, or production runtime configuration were changed.

The initial full-gate attempt was interrupted after diagnosing pytest session cleanup
of the shared temporary-directory root. The retained traceback records that interruption.
The final full-gate attempt uses a private `TMPDIR` under `/private/tmp`; this changes
only temporary-file placement, and excludes no tests.

`focused.log.gz` and `persisted-verdict-red.log.gz` retain the raw console bytes
losslessly, including pytest trailing whitespace. The reproduction script emits
plain `.log` files before this archival compression.
