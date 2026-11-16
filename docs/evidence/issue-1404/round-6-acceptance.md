# Round 6 acceptance readback

Checked 2026-11-16 at 09:02 UTC on repair head
`b89fcc3462890e4892a6bbca2068abd2c0f163e6`.

Outcome: **0/2 outstanding acceptance criteria verified**. A1 and A3 remain
unchecked. No dependency pins, production code, scheduling, or target ownership
were changed in this round.

## Operational acceptance and stop rule

The required outcome is a successful `verify.py aggregate + combined coverage`
job on regenerated #1403, followed by a sync-core classification without the
pin-conflict finding. Read the actual run with:

```sh
gh run view <regenerated-run-id> --repo stranske/Orchestrator --json headSha,conclusion,jobs
```

Stop after one evidence pass and one focused test invocation, within 20 minutes.
Do not substitute repair-PR CI or a local simulation for either live criterion.

## Observed evidence

- #1403 remains open and ready at original head
  `125965ca868ceb44aff1b6fb5123ffe302d7b3da`, based on
  `d43b18be394cb0e113b1e3c79b5ee6016169a115`. The branch's run list contains
  no regenerated CI run.
- `gh run view 43820436609 --repo stranske/Orchestrator` confirms
  [the original CI run](https://github.com/stranske/Orchestrator/actions/runs/43820436609)
  failed, including both aggregate jobs. The named combined-coverage job is
  [133143654504](https://github.com/stranske/Orchestrator/actions/runs/43820436609/job/133143654504).
- `gh run view 43832017069 --repo stranske/Orchestrator` confirms
  [repair-head CI](https://github.com/stranske/Orchestrator/actions/runs/43832017069)
  succeeded on `b89fcc3462890e4892a6bbca2068abd2c0f163e6`, including both
  aggregate jobs. This is supporting evidence, not A1's required regenerated run.
- `/opt/anaconda3/bin/python3 -m pytest -q tests/test_ci_gate_config.py`
  exited 0: **21 passed in 8.88s**. This includes the regression that runs the
  real baseline test against the proposed payload, deliberately breaks Ruff,
  Coverage, and both together, and restores each variant to passing (A2).
- The repository-wide command is `python3 src/verify.py`. It was **not run
  locally**, following this round's explicit prohibition on full-suite runs.
  Therefore A1's local repository-command requirement is also unverified.
- The #1403 conversation contains no later sync-core clearance. This does not
  prove no private round exists; A3 remains **UNKNOWN** without its native result.

## Blocking and drainable work

Blocking: #1403 still tests the original source; A1 and A3 cannot be checked from
this repair branch's green CI. Drainable: the existing closer can process #1405
through its ordinary review and guarded merge; Workflows Maint 52 owns generated
pin updates and can regenerate #1403 after the source lands. The controller-owned
sync-core round then supplies the classification needed by A3. This seat does
not write the generated pin branch or launch a competing lane.

Resume acceptance readback after regeneration: bind the new head to its CI run,
inspect the named aggregate job with `gh run view`, and retain the next native
sync-core result. The local full-suite restriction remains an explicit limitation.

This documentation-only push requires the scheduled post-push review pass after
the seven-minute floor; the automation does not wait or claim that pass complete.

## Round 7 readback — 2026-11-16 09:15 UTC

**0/2 outstanding criteria verified.**

- **A1.** The CI run list for `deps/sync-dev-versions-f5d01d8ca39f` still contains
  only run `43820436609`. A fresh `gh run view` confirmed that the named job
  failed on the unchanged #1403 head `125965ca868c`. The same job passed on
  repair head `cfe6985b7da0` in
  [run 43836289749](https://github.com/stranske/Orchestrator/actions/runs/43836289749).
  The focused local gate file passed all 21 tests. This round's instructions
  prohibit running the full repository test command locally.
- **A3.** The native sync-core journal records #1403 as `pin-conflict` with
  terminal disposition `pin-held` in round `20261116T091416Z`, completed at
  09:14:19 UTC. The combined-coverage failure is still listed, and the handoff
  remains open on #1404.

The delivery step still pending is the landing of the measured baseline from
#1405 before the pin writer regenerates #1403. Recheck the generated head's CI
and the first native sync-core classification of that head.

## Round 8 readback — 2026-11-16 09:32 UTC

**0/2 outstanding criteria verified.** #1403 is still at `125965ca868c`.
`gh run view 43820436609 --repo stranske/Orchestrator` confirms that its only
CI run failed the named aggregate job. The
[coverage shard](https://github.com/stranske/Orchestrator/actions/runs/43820436609/job/133142044085)
and [flat exec-mirror shard](https://github.com/stranske/Orchestrator/actions/runs/43820436609/job/133142044028)
each report one failed test, `test_baseline_was_measured_with_the_pinned_versions`,
for Coverage 7.17.3 versus 7.17.2 and Ruff 0.17.5 versus 0.17.4. The
[aggregate log](https://github.com/stranske/Orchestrator/actions/runs/43820436609/job/133143654504)
reports that shard 1/6 supplied no coverage data. This identifies the first
failing test upstream of the aggregate.

On repair head `9961b14b1e3e`, the named job passed in
[run 43838261567](https://github.com/stranske/Orchestrator/actions/runs/43838261567),
and `/opt/anaconda3/bin/python3 -m pytest -q tests/test_ci_gate_config.py` passed
all 21 tests in 8.75 seconds. The full local repository test command remains
unrun under the round's focused-test rule.

The latest native sync-core round, `20261116T093115Z`, finished at 09:31:18 UTC
and still records #1403 as `pin-conflict` / `pin-held`. The next required
delivery step remains the guarded landing of #1405 so the pin writer can
regenerate #1403.
