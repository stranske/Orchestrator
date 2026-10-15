# Source579: preserve paid retry accounting

Related to #579, following merged #594/#597. A retry that obtained a backend ID and then failed during cost lookup discarded the new identity; successful retries could also replace the old paid primary without retaining it. Cost summaries ignored `retry_attempts` entirely.

The repair retains old and new paid identities on failure, success and post-dispatch exceptions. Cost refresh traverses retained histories and persists identities even when accounting is unavailable. Summaries count each known backend identity once, prefer measured receipts over estimates, include nested historical attempts, and keep one dispute/grade denominator per primary case. Legacy `cost_*_cases` keys continue to describe the cost-attempt population; retries never become adjudicated cases. Imputed dollars remain estimates; unknown billed dollars remain unknown. Malformed history refuses to produce an understated summary.

Exact original main `f25335587cacb1164a808dc03740f3b608b9f993` with the unchanged fifteen new tests overlaid: 15 causal test failures (assertions, explicit guards and missing-history errors; no collection or launcher failure), exit1. Candidate:111 focused PASS and23 subtests, exit0. RED/GREEN logs (trailing whitespace normalized; original hashes in proof.json) and hash/command binding are retained beside this file. The tests use fake dispatch results, a private report and stub cost receipts; no provider or live Brain write occurs.

Commands:

```
python -m pytest tests/test_adjudicator_retry_history.py -q -o addopts=
python -m pytest tests/test_adjudicator_retry_history.py tests/test_adjudicator_retro.py tests/test_adjudicator_packet_binding.py tests/test_adjudicator_retro_evidence.py -q -o addopts=
python -m pytest --collect-only -q -o addopts=
python -m black --check src/adjudicator_retro.py tests/test_adjudicator_retry_history.py
python -m ruff check src/adjudicator_retro.py tests/test_adjudicator_retry_history.py
python -m mypy src/adjudicator_retro.py
python src/adjudicator_retro.py --selftest
```

Collection3448 equals main3433 plus15 independently exercised controls. All skip/selftest/gate/mypy ceilings are unchanged; hosted full CI is still required. Source579 remains open for authentic original native qualification, regrading and October18/November3 observations. This does not dispatch a replacement trial, modify model policy, publish a deployed mirror or establish billed spending.

Native review on initial published d6619d3 found two further boundaries: accounting failure after a valid proposal must not cause redispatch, and withdrawn historical receipts must clear both direct and reported measured amounts. Four new controls assertion-fail on that initial published head and pass after repair. Every attempt refreshes independently; primary decisions alone are regraded.

The subsequent native review identified pending role-record recovery hidden in history. Recovery now traverses retained attempts under the original limit, repairs the original paid record without redispatch and keeps primary grading unchanged. Two abstained/decided-primary controls fail on priorpublishedbacf8f4, then pass after recovery, including failure/retry of the backfill itself.
