# Malformed context recovery

At base head `051d72776d2f52ae662eff69cd657e1fce52e73d`, all 16 new malformed-context regressions fail: missing/null contexts, missing fields, invalid nodes or pagination data, and absent continuation cursor, each through initial and paginated reads. The shared production reader now raises recoverable `ValueError` before either caller consumes malformed data.

The actual production break replaced only `src/adjudicator_retro.py` with that original Git blob while retaining the new tests: 16 failures, exit 1. Exact byte restoration gives 16 passes, exit 0. Hashes and raw phase logs are retained here. Focused suites report 121 passes and 23 passing subtests; the module selftest reports seven checks; mypy, Ruff and Black pass. Actual collection is 2,623. The floor is the previous 2,607 executed nodes plus these 16 executed regressions; all ceilings stay unchanged. This is not a new full local verifier or deployment claim. Hosted `verify.py` on the pushed head remains required.

The original manifest and README are preserved byte-for-byte here. The parent manifest changes only its current README hash; historical phase captures and their original claims remain intact.
