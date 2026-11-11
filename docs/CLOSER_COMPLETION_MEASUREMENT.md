# Native completion measurement

The closer campaign metric counts a completed, independently verified canary.
Its original worker, origin, verification and completion timestamp remain frozen.

The native closer evidence entry (`closer_csv_completion.record_completion`)
captures the authenticated exact-head guarded publication in a separate snapshot
bound to the original receipt digest. Repeating this evidence entry refreshes the
observation without rewriting the receipt or rerunning its worker. A failed
refresh leaves the observation unavailable until that native entry succeeds.

Metric queries read and validate this snapshot, the frozen source hashes and the
verification transcript locally. They make no GitHub calls and cannot create or
repair a snapshot. A missing, modified or receipt-mismatched snapshot produces
UNKNOWN and requires the native evidence entry. Changes observed by that entry
invalidate the derived snapshot; a query never substitutes a new observation.
This historical completion signal does not assert current fleet health or grant
merge, worker or acceptance authority.
