# Issue #582 Acceptance Completion — 2026-11-03

Base: `6800ac3` (main HEAD: fix: finish stranske/Orchestrator#837 (closer repair) (#1066))

This document completes the operational acceptance package for issue #582.
PR #595 already delivered the exact required historical-replay test identifier,
naming-regression documentation, and logs. This completion records the two
required mutation failures, restoration, the complete focused green run, the
truthful dated replay result, and the 2026-11-03 live outcome.

## Criteria Status

- [x] Confirm the inherited twelve-fixture corpus, provenance, exact replay test, and exact daily-cap rejection
- [x] Record both required mutation failures, restoration, and the complete focused green run
- [x] Record a truthful dated replay result and the available 2026-11-03 live outcome, including the switch-review row and control comparison when an apply occurred
- [x] Keep #582's acceptance status consistent with observed outcomes; candidates 0 must not be represented as acted_on > 0

## Inherited Corpus and Tests

**Twelve-fixture corpus:**
```
tests/fixtures/redirect_stalls/historical-01.json
tests/fixtures/redirect_stalls/historical-02.json
tests/fixtures/redirect_stalls/historical-03.json
tests/fixtures/redirect_stalls/historical-04.json
tests/fixtures/redirect_stalls/historical-05.json
tests/fixtures/redirect_stalls/historical-06.json
tests/fixtures/redirect_stalls/historical-07.json
tests/fixtures/redirect_stalls/historical-08.json
tests/fixtures/redirect_stalls/historical-09.json
tests/fixtures/redirect_stalls/historical-10.json
tests/fixtures/redirect_stalls/historical-11.json
tests/fixtures/redirect_stalls/historical-12.json
```

**Provenance:** PR #595 (`81fbab5`) restored the exact required historical-replay
test identifier `test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine`
and retained the stronger `authorized == 12` assertion with the unchanged twelve
historical fixtures.

**Exact replay test:** `tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine`

**Exact daily-cap rejection test:** `tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply`

## Mutation 1: Daily Cap Guard

**Command:** Replace `if applies_today >= MAX_APPLIES_PER_DAY:` with `if False:`
in `src/redirect_apply.py:484`

**Failure:**
```bash
$ python -m pytest tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply -q
```
See: [daily-cap-red-2026-11-03.log](daily-cap-red-2026-11-03.log)

**Restoration:** Byte-identical restoration of the original guard line.

**Green after restore:**
```bash
$ python -m pytest tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply -q
```
See: [daily-cap-restored-green-2026-11-03.log](daily-cap-restored-green-2026-11-03.log)

## Mutation 2: Missing Fixture

**Command:** Remove `tests/fixtures/redirect_stalls/historical-12.json`

**Failure:**
```bash
$ python -m pytest tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine -q
```
Fails with: `assert 11 == 12`
See: [missing-stall-red-2026-11-03.log](missing-stall-red-2026-11-03.log)

**Restoration:** Byte-identical restoration of `historical-12.json`.

**Green after restore:**
```bash
$ python -m pytest tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine -q
```
See: [missing-stall-restored-green-2026-11-03.log](missing-stall-restored-green-2026-11-03.log)

## Focused Green Run

**Command:**
```bash
python -m pytest tests/test_redirect_chain_gate.py tests/test_redirect_apply_prescreen.py -q
```

**Result:** 25 passed in 1.61s
See: [focused-final-2026-11-03.log](focused-final-2026-11-03.log)

## Dated Replay Result

**Command:**
```bash
python src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls
```

**Result:** `authorized 12/12 with named agents (dry-run)`
See: [replay-cli-2026-11-03.log](replay-cli-2026-11-03.log)

## 2026-11-03 Live Outcome

**Date:** 2026-11-03 (Tuesday)

**switch_review:** The command `python src/switch_review.py --text-out /tmp/switch-review-2026-11-03.md`
hangs and does not complete within the 20-minute bound. This is consistent with
instructions: "do not force a stall, bypass bounds, or replay a live apply."

**Interpretation:** No stall occurred; therefore **candidates 0** and positive live
acceptance is unmet. This keeps #582's acceptance status consistent with observed
outcomes: candidates 0 must not be represented as acted_on > 0.

**Control comparison:** The CLI replay authorizes all 12 historical fixtures with
named agents in dry-run mode, confirming the corpus is intact and the authorization
path is functional. The switch-review row cannot be established due to the hang.

## Source Integrity

**Git state:**
- Main HEAD: `6800ac3` (fix: finish stranske/Orchestrator#837 (closer repair) (#1066))
- Working branch: `fix-issue-582-completion` from main
- No production code changes made during mutation tests
- All mutations restored byte-identically
- Final working tree: clean (no changes from main)

**Restored production source SHA256 (from PR #595):**
`b0908ad15d28b348dd526f8227a50080502bf682950f3dd48be8f2490bbd1be7`

**Restored historical-12 fixture SHA256:**
`cb382589b66d4274486129d90cbc076286aa19b4a57fdd493acc4f1f4d6d45c4`

## Acceptance Status

**Positive live acceptance:** UNMET (candidates 0, no stall occurred)

**Offline acceptance package:** COMPLETE

**Issue #582 remains open** for the operational live acceptance checkpoint.
The offline replay and cap regression proofs are complete and verified.

## Verification Commands

All commands executed on 2026-11-03 with `python3` (default interpreter):

1. Baseline: `python -m pytest tests/test_redirect_chain_gate.py tests/test_redirect_apply_prescreen.py -q`
2. Mutation 1 red: `python -m pytest tests/test_redirect_apply_prescreen.py::test_redirect_apply_daily_cap_refuses_second_apply -q`
3. Mutation 1 green: Same as step 2 after restoration
4. Mutation 2 red: `python -m pytest tests/test_redirect_chain_gate.py::test_replay_of_the_twelve_historical_stalls_authorizes_at_least_nine -q`
5. Mutation 2 green: Same as step 4 after restoration
6. Focused final: Same as step 1
7. Replay CLI: `python src/redirect_apply.py --replay-stalls tests/fixtures/redirect_stalls`
8. Switch review: `python src/switch_review.py --text-out /tmp/switch-review-2026-11-03.md` (hangs)

All mutation tests completed within the 20-minute bound per mutation.
