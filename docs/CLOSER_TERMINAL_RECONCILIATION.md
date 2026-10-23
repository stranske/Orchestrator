# Terminal-owner reservation reconciliation

This extends the existing closer resource reconciliation rail. Deduplication found
`plan_legacy_reconciliation` already protects causally bound no-operation waits;
`unknown_reservations` and `_expired_admission_diagnostics` had no supported path
for old unbound legacy advertisements or Core intents after their owner exited.
The historical dormancy inventory, current capability inventory and improvement-log
reservation search found no second reconciliation executor to reuse.

Use the existing CLI to make a read-only plan:

```sh
python3 src/closer_resources.py --state /absolute/pipeline.json --target owner/repo#N --terminal-owner
```

Only an eligible plan supplies `plan_sha256`. Explicit application rechecks owner
process and process-group absence, original ledger and resource locks, current
remote snapshot and all-head native runner reservations. A changed remote revision,
intent, live/unknown owner, held lock or incomplete remote inventory refuses.
Source issues include every referenced delivery PR, including unmerged PRs. Each
recorded workflow attempt is inspected directly across all provider implementation
and push jobs; malformed jobs/steps or admitted work without terminal evidence
refuse reconciliation. Local owner and exact intent bytes are checked again after
remote observation:


```sh
python3 src/closer_resources.py --state /absolute/pipeline.json --target owner/repo#N --terminal-owner --apply --expected-plan-sha256 PLAN_HASH
```

The immutable audit retains original intent bytes and SHA-256 hashes plus the
fresh observed target state. Each intent is atomically moved to a `.retired`
file only after the audit is fsynced. A crash after the audit write and before
the first rename resumes from that same audit when the plan hash and
advertisement bytes still match (`observed_at` is ignored). A partial archive
after at least one rename leaves remaining intents blocking: plan those
remaining bytes afresh, preserving prior audit files. No live reservation is
expired, overwritten or silently deleted.

Retirement releases resource advertisements only. The original operation and
pipeline ledgers are unchanged; UNKNOWN is not rewritten as success. The result
explicitly grants no acceptance, global-stop or effect-retry authority. Existing
operation reconciliation, current-head merge/review gates and admission still
apply. In particular this does not repair `no_pipeline_continuation_proof` or
promise `authority: true` merely because the reservation count falls to zero.
Expired-admission receipts grade that outcome as `reservation_drain_pass`
(`reservation_drain_pass_number`, check date 2026-10-30): before the date the
status stays `pending` while `reservation_cleared` reports whether
`blocking_reservations` is 0 or equals `drainable_reservations`. After the date
both that clearance and `authority: true` are required to pass.
No production reservation was migrated or retired during implementation.

New legacy reservations require either a pipeline-run binding or the existing
native-state path plus operation ID. Raw native execution now supplies that
binding instead of creating another unbound legacy advertisement. The prior
causally bound no-operation reconciliation API remains available unchanged.

## Validation

`python3 -m pytest -q tests/test_closer_dispatcher.py tests/test_closer_entry_gates.py tests/test_closer_legacy_advertisement_reconciliation.py`

The named `test_dead_owner_reservations_become_drainable` covers one unbound
legacy record and one dead Core owner, live remote read on planning and applying,
empty reservation readback, exact archived bytes/hash and unchanged UNKNOWN ledger.
`test_live_owner_reservation_stays_blocking` proves a live owner prevents even a
remote read. Negative controls cover remote failure, pending native writer,
changed remote revision, a live process group, original held locks, malformed
owner identity, symlinked intent, incomplete issue timelines, other-provider
implementation jobs and local-owner/intent changes during remote observation.
All cases use temporary state.
