# Case-level native recovery plan

Recorded 2026-11-04 UTC before execution on checkout `fd62737`.
The first unchecked retry task remains blocked on original manifest recovery,
authenticated native receipts, independent truth and live writer ownership.
Read-only inspection of issues #1072, #1096 and the original #579 receipt found
references, not the original 21-row manifest. Bounded Dropbox title searches for
the original-results, original-before, continuation-meta and paid-lineage files
did not recover those artifacts. Search absence is not proof of deletion.

Implement case-level unresolved native journal locators for uniquely matched
supplied retry identities. Include reservations, invocations without backend IDs,
persisted raw responses and unrecognized states. Keep these separate from
backend-bound receipt matches and the primary saved attempt's disposition.
Case identity alone cannot bind a journal attempt to a retained invocation.
Ambiguous requested identities cannot borrow candidate journal evidence.

Run `node --test tests/test_adjudicator_inventory.js` before and after the fix;
expect the new controls to fail before implementation and all inventory controls
to pass afterward. Then run the acceptance-runner and evidence-ledger regressions,
syntax and whitespace checks. Preserve commands, logs and source hashes.

Stop after local fixture verification; do not invoke a provider, publish a
canonical report, change production storage, or construct a replacement cohort.
The selected CI Python 3.14 interpreter still lacks pytest; prior required-test
failures remain unresolved and are not rerun solely for this JavaScript change.
No operational acceptance checkbox can be completed from fixture results.
