# CSV canary contract verification (reproducible)

- **Issue**: [stranske/Orchestrator#988](https://github.com/stranske/Orchestrator/issues/988), which remains open for native corrective-controller acceptance.
- **Partial revision**: commit `05a7ce20246df00b09daf091d16e5f25c13e96a7` (PR #984).
- **Corrected exporter revision**: commit `a30b34ada04a30d04360867e14097a8c5b6d731c` (PR #1168, already on `main`). PR #989 adds contract checks, not another exporter implementation.
- **Checked**: 2026-11-05, using `/opt/anaconda3/bin/python3` (Python 3.12.2).

Native Verify outcomes for this work are the PR's **Verify** CI check and the post-merge verifier. The local results below do not substitute for either, or prove native worker execution or PR publication tied to the corrected revision. Native corrective-controller acceptance remains unresolved under #988; PR #989 does not close it. The existing `--chain-evidence` reader is unchanged by this salvage.

## Pytest must not collect the fixture tree

The script explicitly sets `__test__ = False` and defines no `test_*` functions.

```bash
/opt/anaconda3/bin/python3 -I -m pytest --collect-only tests/fixtures/closer_real_csv_canary -q
```

Observed: `no tests collected`, exit **5** (pytest's no-tests-collected status).

## Partial revision: contract script fails

Run from the repository root. A fresh temporary directory prevents stale revision contents from being reused.

```bash
partial_dir=$(mktemp -d /tmp/pr989-partial.XXXXXX)
git show 05a7ce20246df00b09daf091d16e5f25c13e96a7:tests/fixtures/closer_real_csv_canary/v1/csv_export.py > "$partial_dir/csv_export.py"
/opt/anaconda3/bin/python3 -I tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py "$partial_dir/csv_export.py"
```

Observed: exit **1**. The traceback contains:

```text
NotImplementedError: Declared incomplete CSV canary fixture
AssertionError: CSV contract is not implemented
contract assertion failed
```

## Corrected revision: contract script passes

Use the same script with the exact corrected exporter revision:

```bash
corrected_dir=$(mktemp -d /tmp/pr989-corrected.XXXXXX)
git show a30b34ada04a30d04360867e14097a8c5b6d731c:tests/fixtures/closer_real_csv_canary/v1/csv_export.py > "$corrected_dir/csv_export.py"
/opt/anaconda3/bin/python3 -I tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py "$corrected_dir/csv_export.py"
```

Observed: `contract ok`, exit **0**. The default sibling-exporter invocation also prints `contract ok`, exit **0**:

```bash
/opt/anaconda3/bin/python3 -I tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py
```

These checks cover exact JSON serialization, extra fields, Unicode, empty JSON and CSV input, unsupported formats, fixed CSV headers, CRLF record endings, and comma, quote, LF, and CR round trips in CSV fields.

## Current closer-core acceptance — owner scope update, November 8

The owner authorized equivalent evidence from current closer-core in place of the retired desktop closer. Historical results above remain intact. `--chain-evidence` now reads the current native receipt; `--legacy-chain-evidence` retains the historical reader.

The actual closer-core worker log for delivery PR #989 contains a successful commit/push to `85adde2614db7118d6e6d28dd11e5bc4c7611319`, a subsequent exact-head readback and a terminal worker result. The native [guarded merge acknowledgment](https://github.com/stranske/Orchestrator/pull/989#issuecomment-6684682838) names that same head. Its published merge is `97647baf97085724af7cb8f684af2331f73fb612`.

A candidate run on November 8 used `runtime_ac_gate.recheck_merged` on an actual disposable checkout of that exact merge. The selected virtual-environment interpreter ran `python -I tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py`: stdout `contract ok`, exit0, native gate PASS. This is separate postmerge execution, not the earlier worker's reported test result. The receipt freezes the original worker and dispatch-origin bytes; subsequent observations reuse that result and validate the current remote identity.

The measuring query counts this registered canary; it does not report fleet throughput. Missing or changed evidence stays unmeasured. Deployment and the normal native evidence entry remain required before closing #988 or #1079.
