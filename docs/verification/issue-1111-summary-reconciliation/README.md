# Issue 1111: saved summary reconciliation

Task 5 advances; all operational task and acceptance checkboxes remain unchecked.
The first four tasks remain blocked by the original manifest, permitted Brain,
selected pytest environment and real contested observation requirements already
recorded in the preceding recovery receipts. The fresh
[runner inventory](runner-inventory.json) again cannot open the default Brain;
its population, eligible count and discrepancy remain null. No owner-excluded
repository was accessed. Existing tooling is retained unchanged.

The new local, read-only comparator accepts these retained inputs:

```sh
node scripts/reconcile_adjudicator_summary.js --report SAVED_REPORT --case-gaps PERSISTED_CASE_EXPORT --switch-review CAPTURED_OUTPUT --python SELECTED_INTERPRETER
```

The case export comes from the existing `export_adjudicator_case_gaps.js` with
`--persisted-roles`. The comparator checks report and row digests before using
exported role matches, and checks their target, role/backend IDs and decision.
It counts a shared whole-run backend receipt once, leaves conflicting amounts
unknown and keeps absent or zero store telemetry unknown. Current role receipt
subtotals do not become complete costs: retained attempts and the binding to the
original dispute and revision remain explicit gaps.

For reports with case rows, the selected interpreter invokes the production
`summarize()` and `weekly_line()` readers. The result retains its executable,
version and source hashes. Every recomputed summary field is compared with the
saved value; missing legacy fields and mismatches are listed separately. A
single adjudicator line in supplied switch-review output must match the saved
renderer exactly. That match cannot validate a corrupted saved summary, and
neither comparison proves historical execution. Owner-excluded case, inventory
or retry identities withhold execution claims and the whole-report recount.
Input hashes are checked again before returning evidence. There is no dispatch,
Brain connection, report write or publication in this comparator.

The [actual historical result](historical-reconciliation.json) retains the
37-case/122-population aggregate, its original null cost and zero grades. The
artifact contains no case rows. No corresponding persisted case export or
actual switch-review capture was recovered. All three missing inputs are named;
row count, unknown-cost case count and complete cost remain explicitly unknown.
No manifest or switch output is synthesized from the aggregate. CLI exit 2
means incomplete evidence, even when supplied files agree; malformed inputs or
a failed selected interpreter exit 1 without partial JSON.

[Validation](validation.json) binds the base revision, source and input hashes,
commands, exits and raw logs. Four test files pass: the nine new comparator
regressions plus the existing case-gap, inventory and retrospective-batch suites.
The focused production switch-review reader/renderer test also passes. New
regressions exercise aggregate corruption even when rendering repeats it,
stale/ambiguous/missing row bindings, unexpected exported identities, shared and
conflicting receipts, zero telemetry, unresolved retry costs, owner exclusions,
aggregate-only inputs, CLI failures and byte-identical inputs with no sidecars.
All of these execution witnesses use private fixtures. They do not establish
original-cohort, real closer, named safety mutation or operational acceptance.
No Python files change, so the Python pre-commit formatting gate does not apply.
The two previously diagnosed full CLI failures remain outside this change.

No remote metadata writes or PR lifecycle changes were attempted. Successor
ownership remains [issue #1111](https://github.com/stranske/Orchestrator/issues/1111)
and [PR #1112](https://github.com/stranske/Orchestrator/pull/1112). Recover the
original identity manifest and permitted Brain, then retain the full saved
report and actual switch-review capture. Run the comparator with the selected
repository interpreter and resolve its case, attempt, revision and cost gaps
before marking summary reconciliation complete.

Repository staging failed because `.git/index.lock` is on a read-only mount.
The source/test commit is retained with separate Git metadata under
`/tmp/issue-1111-summary-delivery.git`; its transfer patch and commit identity
are retained in `source-commit.patch.gz` and `delivery.json`. The checkout branch
is not advanced; workspace files remain available for automation to collect.
