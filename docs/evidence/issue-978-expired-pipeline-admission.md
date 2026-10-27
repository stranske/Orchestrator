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

The hosted runner reported read-only `.git` and connector publication restrictions during
its local execution. The workflow subsequently committed and pushed this follow-up
as `324f890d1f27ce5b003b25ce5bd5f9c64c27e9ab`. Those historical runner restrictions
are not a current publication blocker; the earlier hosted checks do not validate
that later revision.

## Remaining live acceptance

### CLI admission follow-up (2026-10-27, original runner evidence)

The existing CLI budget receipt test now covers both a valid prior stop and an expired
prior run with an invalid stop. The latter case requires
`legacy_window_expired_fresh_admission`, a future selected deadline, false/zero expiry
diagnostics, and unchanged legacy and pipeline state bytes. This is an additional
test change; the production admission and diagnostic changes are already present.

This runner has neither pytest nor Black installed. The targeted dispatcher/pipeline
test command failed before collection (`No module named pytest`). Both the changed-file
Black invocation and the required repository-wide Black check could not run because
Black is absent. Installing the tools into `/tmp` failed because PyPI DNS resolution
is unavailable; no existing local installation was found. The changed test module
parses and `git diff --check` passes, but those checks do not establish test success.
The workflow committed and pushed this test follow-up as
`4abbcef0065e30f95a441eb7bddc574a5b7691e3` despite the runner's local tool failures.
The earlier hosted checks cover only `4b3eec116e0305d5df9c793da6257789a15ab1db`;
they do not validate this committed revision. Its own hosted tests subsequently
failed because the new invalid-stop case incorrectly expected exit zero. No
additional task checkbox is complete on the strength of the unverified follow-up.

### Opener recovery (2026-10-27)

The committed CLI regression failed on hosted Python 3.13 and 3.14 in Gate
40566689216 and checkout/flat-mirror shard 3 in 40566688830. The fresh deadline
was correct; the preserved invalid prior stop correctly made the CLI return 78.
The test now separately asserts exit 0/valid stop for the valid-stop case and
exit 78/invalid stop with `unverified_deferral:o/r#7` for the expired invalid-stop
case. Both cases still assert future admission diagnostics and unchanged legacy
and pipeline bytes. Production code and live runtime state are unchanged.

Using `/opt/anaconda3/bin/python3` (Python 3.12.2):

- Original committed regression: 1 failed, 1 passed (`assert 78 == 0`).
- Corrected exact regression: 2 passed.
- `python3 -m pytest tests/test_closer_dispatcher.py tests/test_closer_pipeline.py -q -m 'not slow' --disable-warnings --maxfail=1`: 511 passed in 50.32s.
- `python3 -m black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`: 818 files unchanged.
- `python3 -m ruff check tests/test_closer_dispatcher.py` and `git diff --check`: passed.

Deliberately returning success from the production CLI even for an invalid stop
made the corrected regression fail (`assert 0 == 78`). Exact-byte restoration
returned both cases to green. This proof used isolated temporary fixtures, with
no live closer or installed-mirror invocation. New hosted Python 3.13/3.14 and
flat-mirror checks must validate the recovery commit independently.

### Review follow-up: independently reachable operation gate (2026-10-27)

Head `65473099859d971da83628d1a70d9b55d000e1b9` passed hosted
[CI 40574625177](https://github.com/stranske/Orchestrator/actions/runs/40574625177)
and [Gate 40574626304](https://github.com/stranske/Orchestrator/actions/runs/40574626304).
The submitted CodeRabbit review identified a remaining test gap: the invalid-stop
regression returns before exercising the unresolved-operation gate. The documentation
thread is resolved, but this separate finding needed implementation.

The existing parametrized operation-barrier test now runs the real resource-scoped
scheduler with a valid prior pipeline stop and the selected fresh deadline. For
submitted, running, and uncertain original operations, target admission refuses with
`scope_quarantined`. Its retained scope proof names the original native queue item,
reports a quarantined resource with no malformed-source reasons, and records that no
callback started. All stage callbacks fail if called. The original queue and pipeline
ledger remain byte-identical, the prior pipeline stop remains valid, and no mutation
reservation is created. The separate direct operation-dependency assertion remains.
The invalid-stop scheduler regression is also retained. No collected cases were added.

Local Python 3.14.8 validation with pytest 9.1.1 from cached wheels:

- Focused operation-barrier regression: **3 passed**.
- `python3 -m pytest tests/test_closer_dispatcher.py tests/test_closer_pipeline.py -q -m 'not slow' --disable-warnings --maxfail=1`: **511 passed** in 27.25s.
- Black 26.10.1 formatted the changed file without edits. The required repository-wide
  `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`
  passed: **818 files would be left unchanged**. The sandbox blocks multiprocessing
  and the single-worker asynchronous check stalled, so all 818 files first passed
  individual Black CLI checks, populating Black's normal cache. The unchanged
  repository-wide command then passed with that cache. Formatter code and rules
  were unchanged. `git diff --check` also passed.

This runner could not stage the follow-up: `git add` failed because `.git/index.lock`
is on a read-only filesystem. The GitHub connector also refused both the requested
`needs-human` label and blocker comment because those mutations require approval
and the current approval policy is `never`. The test change remains uncommitted;
publication requires a runner with writable Git metadata. PR #982 was verified open
and ready for review (`isDraft=false`) at head `6547309`.

The hosted green checks above validate the preceding head, not this test follow-up.
CI, delayed review, guarded merge, and post-merge verification remain pending. The November 3 live pass number requires a real subsequent closer receipt; local tests do not establish it. Source #978 remains open. This PR does not update the installed mirror, run a live closer, change schedules/models, or release any writer/reservation.
