# Ship-gate legacy repair and provider disposition (#415)

This follow-up preserves the strict rule: only a promotion that reaches a terminal phase during
the current followup run touches the global stamp or increments `finished`. An already-terminal
promotion can repair its missing local `ship-gate.json` without taking another hold. The initial
repair is commit `91d5608a9bf3185aa99355fa701fabc676cca643`; this follow-up extends its regression
and verifies the formatter's observable behavior.

`stamp_age_s` starts with the age measured at run entry and refreshes to zero after a genuine
finish. `launch_available_at_start` remains the entry-time measurement. Tests cover both a stamp
created by that finish and a two-day-old stamp refreshed by it: the line says
`finished 1` and `stamp 0.0h old, holds 24h`. Local-only recovery retains the old age, does not
count a finish, and leaves the next tick able to launch; it also preserves the recovered checkpoint.

## Provider concern disposition

Source: [#409 provider comparison, comment 5983266445](https://github.com/stranske/Orchestrator/pull/409#issuecomment-5983266445),
evaluating `c88282b063d6b79efbac2103e5769166ea7b27f2` with reported head
`e85315751586e9509c041fad96c7844670bdc89b`. Both providers returned **CONCERNS**.
O1–O3 and A1–A6 below follow the concern order in the OpenAI and Anthropic report respectively.

| Concerns | Disposition and evidence |
|---|---|
| O1, A3: missing local checkpoint re-holds an unchanged terminal phase | Repaired locally before returning, with global accounting reserved for a new transition. `test_legacy_missing_local_checkpoint_does_not_rehold_or_recount` removes the checkpoint, ages the global stamp two days, checks its unchanged mtime and zero finishes, then confirms a subsequent candidate launches without extending the stamp. |
| O2: stale same-run summary age | Refreshed to zero only after a genuine finish. Both parameter cases of `test_a_new_finish_holds_the_gate_once` check the formatter's output, local checkpoint, one finish, and unchanged global mtime/zero finishes on the next tick. |
| O3, A1: incomplete changed-code review, including truncated `.verify-floor.json` | The original provider inspection remains **INCOMPLETE** (28,811 of 179,975 changed-code characters). This bounded repair and its tests do not establish complete inspection of #409 or change either provider verdict to PASS. The floor adjustment here records measured collection and preserves all ceilings. |
| O3, A2: acceptance evidence unavailable, including production/smoke evidence | Production deployment, installed mirror behavior, and the original acceptance artifacts remain **unverified**. Local isolated tests and the module selftest are evidence of this repair only. No production PASS is claimed; publication work in #389/#414 remains separate. |
| A4: unreadable state can block launches indefinitely | Retained as a conservative safety policy: unknown state cannot establish that synthesis is idle. The three invalid-state regression cases preserve the file, report the per-experiment error, reconcile readable promotions, and prevent launches. Repairing corrupt production state or adding its recovery policy is outside #415. |
| A5: `inflight yes` also represents an unreadable state | Acknowledged reporting limitation. The existing summary conservatively reflects the launch blocker rather than proving a running synthesis. This follow-up does not change that meaning or claim confirmed running work from it. |
| A6: `launchable` counts eligibility before reconciliation, not a confirmed launch | Retained: `launchable` reports that a launch was available when the candidate was examined; `launched` separately reports the confirmed `synthesis_launched` action. Existing open/held new-evaluation tests verify both counters. No equality is promised when reconciliation declines a launch. |

## Local validation

Validation used Python 3.12.14, pytest 8.3.5, and the repository-pinned Black 26.5.1. The runner
initially lacked pytest and Black and could not resolve package-download DNS; validation tooling was restored under
`/tmp` from official upstream GitHub sources and existing local dependency packages. Nothing
was added to the repository's runtime dependencies.

- `python3 -m pytest tests/test_followup_ship_gate_latch.py -q -m "not slow"`: **22 passed**.
- Deliberate pre-fix control: the same tests import a scratch copy of `src/exp_abcd.py` from
  `b9b6e96a5186de588629e0496feff0ee160bf576`, using pytest's `pythonpath` override. **3 failed,
  19 passed**: legacy repair changes the global mtime; genuine finishes retain `None` or
  `172800` as their reported stamp age. The working source was never replaced.
- Latch, synthesis promotion, research usage guard, and experiment arm identity tests together:
  **73 passed**, with two existing tarfile deprecation warnings.
- `src/exp_abcd.py --selftest`, with runtime/handoff directories isolated under `/tmp`: **OK**.
- `pytest --collect-only -q`: **1,831 collected**; floor changes from 1,829 to 1,831 account for
  the initial legacy regression and the additional formatter parameter case.
- `black --check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .` with pinned
  Black 26.5.1: **316 files unchanged**. The local launcher used spawn workers and periodic
  event-loop wakeups to avoid the runner's asynchronous executor stall.
- `git diff --check`: **PASS**.

## Verified task checklist

- [x] Separate local checkpoint recovery from global hold/finish accounting for an unchanged phase.
- [x] Verify the already-discarded, missing-checkpoint, two-day-old-stamp regression.
- [x] Refresh the final summary after a genuine finish and test formatter output.
- [x] Confirm the focused suite passes and deliberately fails against pre-fix code.
- [x] Confirm a new finish holds once and subsequent ticks do not extend or recount it.
- [x] Account for every #409 provider concern while preserving unavailable production evidence.

Full exact-head CI and fresh provider reassessment remain the responsibility of the PR checks.
