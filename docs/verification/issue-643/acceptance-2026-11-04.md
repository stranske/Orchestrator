# Issue 643 live shadow acceptance — 2026-11-04

Outcome: a new certified dispute produces one advisory completion event, a second
pass produces none, and judged durability produces exactly:

`adjudicator live: disputes 1, advised 1, uncertified 0, graded 1 (agree 1, disagree 0)`

Command (repository has no selected virtual environment):

```sh
/opt/anaconda3/bin/python3 -m pytest tests/test_adjudicator_retro.py -k 'test_live_shadow_adjudicates_a_new_dispute_once_and_grades_on_truth or test_live_shadow_never_adjudicates_an_uncertified_case or test_live_shadow_writes_no_merge_input'
```

Result: 3 passed, 78 deselected. These are private-Brain tests with a stubbed
collector and role backend, not production advice or production grading.

A strengthened existing collector test first failed with source `retrospective`
in a live packet. Qualification now preserves the live source on certified live
rows, so backend records use the same provenance as the advice event.

The live runner calls `live_disputes` with an explicit initial cutoff of the
previous 24 hours and requires unknown later truth before new dispatch. Older
pending rows and already-graded rows are not backfilled as live advice. The named
test includes both historical distractors; before the correction it printed
`assert 3 == 1`. The cursor drains eligible cases oldest first, overlaps the last
processed second for safe deduplication, and retains capped/failed work across
later wall-clock runs. The dispatch-budget test proves the remaining two of five
cases are handled on the following day.

The collector-issued case is checked again at advice persistence. Advice and
uncertified reasons use bounded completion-event payloads, with separate advice
run identities so original outcome/influence inputs are untouched. Brain receipts
recover advice after report loss and override stale failed-attempt caches after
an event-commit/report-replace crash. The strengthened named test first printed
`stale report overrode durable advice` (two events) before this correction. The report's writer lock serializes dispatch and
reconciliation; when busy, the drain is reading the last saved weekly report.

`duplicate-advice-red.txt` records removal of the already-advised exclusion:
`AssertionError: second run wrote duplicate advice events`, `assert 2 == 1`.
`duplicate-advice-restored.txt` records the restored named test passing and the
exact live line above. `focused-green.txt` records the related focused suites.
The full suite was not run locally.

Truth reconciliation runs before dispatch, after the durability sweep through
`orchestrate.sh`'s existing adjudicator-live cadence, and via `--reconcile` even
when dispatch is switched off or GitHub capacity is unavailable. Below 0.75 on at
least 20 graded cases, it writes a dated sticky retirement receipt and disables
further live dispatch. Grading and weekly reads remain available while retired.
The private retirement test exercises 19 versus 20 graded cases and refusal to
dispatch after retirement. It is not evidence of production retirement.

## Sync and weekly integration

The owner's `~/.codex/bin/orch-sync-mirror.sh` calls the repository's
`scripts/build_exec_mirror.sh`; that builder copies `src/*.py` and `orchestrate.sh`.
The existing cadence registry contains `adjudicator-live`, and the existing
`switch_review.py --text-out` live field now consumes dated weekly evidence,
including agreement against 0.75 and retirement status. No additional installer,
schedule, capability row, or merge authority was added. Rollback uses
`ORCH_ADJUDICATOR_LIVE_SHADOW=0`; retained advice still supports truth grading.

The concurrent keepalive writer completed successfully before its commit
369a3a01 was fast-forwarded into this worktree. Its live selection work is retained;
its event target lookup now also supports the independent advice run identities.
A later completed keepalive commit, 4f0e4127, adds six dispatch-budget/switch/timeout
cases and retains timeout failures for bounded retry. Those changes are preserved;
the combined focused run reports 256 passed, 2 skipped and 32 subtests passed.

The collection floor changes from 14196 to 14213: twelve preserved keepalive cases
plus five new live tests. Expected passed changes from 14194 to 14211; all ceilings
are unchanged. These totals are derived from the test delta, not an observed
full-suite pass. CI must verify the combined result.

## Outstanding operational acceptance

Production deployment, live advice and the #575 weekly milestone remain UNKNOWN.
This run did not sync the protected live mirror or invoke production model work.
No production advice, graded cases, agreement, or retirement is claimed by the
private tests.

After the owner's next ordinary sync of the delivered source, dated #575 weekly
evidence must show advised A > 0 within seven days and graded G >= 20 by
2026-11-08, report agreement against 0.75, and record any required retirement.
Those future real disputes and durability outcomes cannot be manufactured by a
coding agent. The issue's operational acceptance must remain open until that
readback exists.

Follow-up outside the acceptance push: the older-head CodeRabbit review also
reported docstring coverage. No unrelated docstring cleanup was attempted.
