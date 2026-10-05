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
