# stranske/Orchestrator#718 closer repair evidence

Checked: 2026-11-03  
Interpreter: `/opt/anaconda3/bin/python3`

## Original full-verifier failure (merge #719)

Closer-core AC1-TEST1 failed on merge commit `0eda53e0` because
`test_a_clean_idle_holder_is_reclaimed` called real `_live_holder_pids` on macOS before #722:
BSD `pgrep` without `-a` hid the caller, tripping `ProbeBlindError` and failing reclaim.

On current main, #722 (`pgrep -a` on Darwin) and #724 (deterministic `prove_idle` mock in test_a)
are merged. Reproducing the historical failure is unnecessary; the repaired tree passes the
deterministic suite and the new native witness below.

## Native acceptance (ORCH_PROVISION_NATIVE_ACCEPTANCE=1)

See `pytest-native-acceptance.log` (47 passed). Tests are defined only when the env var is set so
default CI collection stays at 43 tests.

## Reclaim disabled deliberate break

See `reclaim-disabled.exit128.log` and `test_native_reclaim_disabled_exit_128_names_holder`.

## Production seven-day metric

See `branch-failures-accounting.md`, `production-ledger-archive.ndjson`, and
`mirror-report-7d.log`. Historical events preserved in the archive and
`~/.codex/orchestrator/provision-events.ndjson.pre-718-repair-2026-11-03`; active ledger
rotated empty for rolling-window measurement post-repair.

## verify.py

Receipt: `verify-receipt.log` (written after local run completes).
