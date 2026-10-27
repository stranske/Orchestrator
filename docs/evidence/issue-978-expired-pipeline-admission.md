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

## Hosted CI and review follow-up (2026-10-27)

Hosted checks passed for head `4b3eec116e0305d5df9c793da6257789a15ab1db`:

- [CI run 40557146569](https://github.com/stranske/Orchestrator/actions/runs/40557146569): all six checkout shards, all six flat exec-mirror shards, and both verification aggregates succeeded.
- [Gate run 40557147039](https://github.com/stranske/Orchestrator/actions/runs/40557147039): Black, Ruff, mypy, Python 3.13 and 3.14 tests, test quality, and the final summary succeeded.

Review inspection found no submitted reviews or inline threads. CodeRabbit reported its usage cap instead of completing a review; a successful opt-in workflow is not evidence of a completed review.

The admission regression now passes its selected fresh deadline to the real `closer_pipeline.run_pipeline` scheduler for each submitted/running/uncertain incumbent operation. Every case returns `prior_run_has_no_valid_stop`, with false authority, an invalid prior stop, no target actions, no stage callbacks, and unchanged original state/reservation bytes. This supplements the deadline-selection assertions without adding collected cases or changing production gates.

Local validation: `python3 -m pytest tests/test_closer_dispatcher.py tests/test_closer_pipeline.py -q -m 'not slow' --disable-warnings --maxfail=1`: **510 passed**. Hosted CI and delayed review must still be checked on the follow-up commit; the earlier green runs apply only to the head named above.

Black 26.10.1 formatted the changed test file; the required repository-wide `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .` passed with **818 files unchanged**. The formatter used temporary cached dependencies and a writable temporary cache; no repository dependencies or formatter configuration changed. `git diff --check` also passed.

Publication blocker: this run exposes `.git` as read-only. The GitHub connector also rejected tree creation with `MCP tool call requires approval, but approval policy is never`. The follow-up test/evidence changes remain local and uncommitted; the hosted results above do not validate them.

## Remaining live acceptance

CI, delayed review, guarded merge, and post-merge verification remain pending. The November 3 live pass number requires a real subsequent closer receipt; local tests do not establish it. This PR does not update the installed mirror, run a live closer, change schedules/models, or release any writer/reservation.
