# Source420 corpus headline recovery

Merged PR468 report https://github.com/stranske/Orchestrator/pull/468#issuecomment-5998089596 remains CONCERNS/CONCERNS. Its corpus-headline claim reproduced on ea92f548: a private fixture-only ledger printed experiment_count1, verdict_count1, verdicts_outcome_derived1 despite capabilities_with_evidence0 and resolved_experiment_count0. Ranking and detector isolation already pass; this is a reporting gap.

The corpus report now excludes zero-weight provenance from production verdict totals and keeps production trial totals separate. fixture_experiment_count and fixture_verdict_count preserve visibility. Raw events, fixture counters, ranking, migration and flags are unchanged. The regression checks fixture-only and mixed-production ledgers, including UNKNOWN self-reported share without a production denominator.

Validation: six fixture regressions pass; capability_propensity selftest passes; Black/Ruff and diff checks pass. Existing skip/type ceilings preserved; collection measured on integrated source. Source420 stays open pending guarded squash and post-merge comparison. No live mirror or ledger migration is performed.


## Review recovery: legacy fixtures and text visibility

The existing `_fixture_contract_event` recognizes explicit rail contract identities.
`_events` now uses that identity during read-only classification, before maintenance
has run; ordinary production evidence mentioning a fixture remains production.
The explicit migration inspects recorded provenance and still appends its own
idempotent corrections. Reading the report does not change the ledger bytes.
The text report prints fixture experiment and verdict-event counts beside production.

Regression proof on the prior source (6f5d47b):
```text
FAILED test_unmigrated_rail_contracts_are_fixture_evidence_on_read
FAILED test_formatted_corpus_report_shows_fixture_counts
2 failed in 0.43s
```
Restored/fixed source:
```text
python3.12 -m pytest -q -o addopts= tests/test_fixture_provenance.py
8 passed in 0.56s
```
The legacy test asserts read-time bytes remain identical, migration changes two
records then zero on rerun, and a real production verdict remains counted.
No live ledger migration or source-to-live mirror publication was performed.
