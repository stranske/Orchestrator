# Surface-specific missing facts: acceptance evidence

Source issue #418; implementation PR #490 (squash
`576314847b600c2b84d43fc6b057f5071ab048fc`). The provider comparison run
37476463889 remains **CONCERNS / CONCERNS**. This follow-up supplies the missing
named pytest and production-mutation evidence and replaces the contradictory
historical environment note; it does not rewrite the provider verdict.

Validated on current main `69f3bbb8d5ef8aaa7edbb24ba606d9543145e52f` on
2026-10-06. All tests use temporary ledgers. No live ledger, registry, mirror or
runtime generation was changed.

- [x] Unknown PR preconditions withhold offers and return surface-attributed facts.
- [x] Missing facts use match events with surface/capability/fact metadata, never declines.
- [x] Consult context accepts the seven documented PR fields.
- [x] Detection excludes missing facts from offers/declines and prints surface totals.
- [x] Execute all three exact acceptance nodes with pytest: **3 passed**.
- [x] Execute each required real production mutation and restore exact bytes.

See [the complete command transcript](issue-418-named-controls.txt) for commands,
assertions, exit codes and source SHA256 restoration checks:

| Production mutation | Exact named node | Broken | Restored |
| --- | --- | --- | --- |
| Return false from `_withhold_for_missing_pr_facts`, leaving unknown preconditions offered | `test_unknown_precondition_withholds_the_offer_and_records_fact_missing_on_the_surface` | exit 1, assertion failure | exit 0, 1 passed |
| Count the experiment's `fact_missing` capabilities in `surface_decline_counts` as declines | `test_detect_reports_fact_missing_per_surface_and_never_as_a_decline` | exit 1, assertion failure | exit 0, 1 passed |

The third required node,
`test_known_precondition_true_offers_and_false_declines_as_precondition_unmet`,
passed in the three-node run. The final restored full module passed **11 tests**.
The same module includes surface deduplication, per-surface counts, exception-class
visibility without payload leakage, text rendering, and mixed-task withholding.
Those paths extend existing tests; this documentation follow-up adds no test nodes
and makes no new claim that each extension received an independent mutation.

These receipts disposition the named-test/evidence concerns. Hosted verification
of this follow-up and exact-head review gates remain required before source closure.
