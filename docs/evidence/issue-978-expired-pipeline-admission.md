# Issue 978: expired legacy pipeline admission

Source base: `0d6bc9a1` (2026-10-27). Validation uses isolated temporary state and `/opt/anaconda3/bin/python3`; no live closer execution or runtime installation.

The expired legacy accounting deadline no longer supplies the fallback execution window when all recorded pipeline windows have expired. Malformed/unreadable state and unbound receipts retain their refusal paths. Any active recorded window, including an older one, retains the prior bound. Existing pipeline admission, reservation recovery, uncertain-operation reconciliation, legacy operation dependencies, invalid-stop history, and validate-stop remain unchanged.

## Observed validation

- `python3 -m pytest tests/test_closer_dispatcher.py -q --disable-warnings --maxfail=5`: 258 passed in 18.50s.
- After adding explicit incumbent reservation state to the new regression: focused admission/active-window/deadlock selection: 7 passed.
- `python3 -m ruff check src/closer_dispatcher.py tests/test_closer_dispatcher.py`: passed.
- Installed Ruff format check reports pre-existing formatting differences outside the edited regions; those unrelated lines were preserved.

The named regression covers submitted/running/uncertain original operations, an unresolved original-owner pipeline reservation, unchanged state bytes, an invalid stop that remains invalid, and active-window refusal.

## Deliberate break and restoration

Exact command: `python3 -m pytest tests/test_closer_dispatcher.py::test_pipeline_admission_does_not_select_expired_legacy_deadline -q --disable-warnings`.

Temporarily restored the original `return legacy_bound, "no_pipeline_continuation_proof"` fallback. Observed exit 1, all three operation-status cases failed because the selected deadline was in the past. Restored the implementation byte-for-byte; observed exit 0, all three cases passed.

The receipt records `selected_deadline_expired` and nonnegative `selected_deadline_age_seconds`. The existing CLI receipt test covers both future (false/zero) and retained-expired (true/positive) values.

## Remaining live acceptance

CI, delayed review, guarded merge, and post-merge verification remain pending. The November 3 live pass number requires a real subsequent closer receipt; local tests do not establish it. This PR does not update the installed mirror, run a live closer, change schedules/models, or release any writer/reservation.
