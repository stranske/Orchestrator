# Source420 corpus headline recovery

Merged PR468 report https://github.com/stranske/Orchestrator/pull/468#issuecomment-5998089596 remains CONCERNS/CONCERNS. Its corpus-headline claim reproduced on ea92f548: a private fixture-only ledger printed experiment_count1, verdict_count1, verdicts_outcome_derived1 despite capabilities_with_evidence0 and resolved_experiment_count0. Ranking and detector isolation already pass; this is a reporting gap.

The corpus report now excludes zero-weight provenance from production verdict totals and keeps production trial totals separate. fixture_experiment_count and fixture_verdict_count preserve visibility. Raw events, fixture counters, ranking, migration and flags are unchanged. The regression checks fixture-only and mixed-production ledgers, including UNKNOWN self-reported share without a production denominator.

Validation: six fixture regressions pass; capability_propensity selftest passes; Black/Ruff and diff checks pass. Existing skip/type ceilings preserved; collection measured on integrated source. Source420 stays open pending guarded squash and post-merge comparison. No live mirror or ledger migration is performed.
