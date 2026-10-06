# Surface-specific missing facts

Merged #467 comparison37320679971 remains NON_PASS. The same capability/task
could write only its first missing-fact surface. Separately, a failed ledger
write was swallowed. Existing report aggregation could also cross-count distinct
capabilities between surfaces inside one experiment.

Missing-fact deduplication now includes the surface, and the existing experiment
projection retains capability lists per surface. Repeating the same surface is
still idempotent; consolidated fact-missing data still remains separate from
offers and declines. A failed writer exposes capability, surface and exception
class on the advice response without copying the exception payload.

Actual controls on prior source:
```text
FAILED test_missing_fact_events_deduplicate_per_surface_without_cross_counting
FAILED test_missing_fact_write_failures_are_visible_without_exception_payload
2 failed
```
Fixed source: all11 precondition-withholding tests pass. Both module selftests,
Black/Ruff and collection2102 pass; all prior ceilings are unchanged. The mixed
case records the same capability on two surfaces and another only on the first:
counts are2/1/0 and there are no offers or declines. The failure control raises
an exception containing a synthetic secret and requires only its class to travel.
No live ledger migration, registry update, or mirror publication was performed.

2026-10-06 follow-up: withheld matches now stay out of `not_applicable` when the
same capability matches one classified task type and misses another. Text advice
also prints the missing fact with its consulting surface. Existing tests cover
both reporting paths; no test nodes were added.

- [x] Unknown PR preconditions withhold offers and return surface-attributed facts.
- [x] Missing facts use match events with surface/capability/fact metadata, never declines.
- [x] Consult context accepts all seven PR fields and CLI help names them.
- [x] Detection excludes missing facts from offer/decline counts and prints surface totals.
- [ ] Run the three named acceptance tests with pytest.
- [ ] Run the deliberate-break controls through those named tests and revert.

Independent standard-library checks passed for all four behavior tasks, the
mixed-task reporting regression, and both text rendering paths. Both module
selftests exited zero with private registries; the advisor's live-ledger section
reported its prerequisite absent. Independently breaking unknown-offer withholding
and counting missing facts as declines each raised an assertion; both mutations
were restored and the checks passed again. These controls do not substitute for
the required named pytest run.

Commit remains blocked: pytest and Black are unavailable, and installation from
PyPI failed because network name resolution is unavailable. The required Black
format/check commands could not run. No Python changes were committed and no
acceptance checkbox was marked complete.
