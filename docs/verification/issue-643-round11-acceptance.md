# Issue 643: round 11 acceptance receipt

Check date: 2026-11-11. Evaluated implementation:
`769ba982307273d042be31e3ec4af60de78b1de1` (PR #1356, `orch/issue-643`).

Outcome sought: one private-Brain dispute produces one certified shadow advice,
is not advised again, and is graded when durability arrives, without changing
merge inputs. This receipt does not establish production dispatch or the live
sample-size criterion.

Stop rule for this round: two acceptance attempts or 20 minutes. No production
code was changed. No paid role dispatch or production Brain write was performed.

## Named acceptance tests

Command (no repository virtual environment is selected here):

```sh
/opt/anaconda3/bin/python3 -m pytest -q \
  tests/test_adjudicator_retro.py::test_live_shadow_adjudicates_a_new_dispute_once_and_grades_on_truth \
  tests/test_adjudicator_retro.py::test_live_shadow_never_adjudicates_an_uncertified_case \
  tests/test_adjudicator_retro.py::test_live_shadow_writes_no_merge_input
```

Observed: `3 passed in 0.98s`, exit 0. These tests use a private Brain and stubbed
role backend; they do not prove that a production provider dispatch succeeds.

## Actual mutation and restoration

Extracted `git archive HEAD` into a temporary directory. In that copy only,
replaced the unique line in `src/adjudicator_retro.py`:

```python
seen_targets = live_advised_targets(db)
```

with `seen_targets = set()`, removing the already-advised target exclusion.
Ran the first named test above with the same interpreter. Observed:

```text
tests/test_adjudicator_retro.py:1327: AssertionError
assert retro.live_disputes(db=db, since_ts=0) == []
1 failed in 0.94s
```

Exit 1 was asserted, together with the presence of `AssertionError`. Restored the
original source bytes, asserted byte equality, and reran the same test:
`1 passed in 0.91s`, exit 0. The temporary directory was then removed.

This proves the named test detects removal of the exclusion. It fails at the
direct selection assertion before the second run. It does **not** establish the
literal criterion's failure on the second run with two event rows. Event IDs are
deterministic, and duplicate dispatch can overwrite an existing event.

## Why the automated comparison remains hollow

Reproduced the comparison used by the runtime acceptance recheck:

```sh
/opt/anaconda3/bin/python3 src/local_verify.py \
  --worktree . --base-ref HEAD^ \
  --test-cmd '/opt/anaconda3/bin/python3 -m pytest -p no:cov -q tests/test_adjudicator_retro.py::test_live_shadow_adjudicates_a_new_dispute_once_and_grades_on_truth tests/test_adjudicator_retro.py::test_live_shadow_never_adjudicates_an_uncertified_case tests/test_adjudicator_retro.py::test_live_shadow_writes_no_merge_input' \
  --test-path tests/test_adjudicator_retro.py --timeout 90 --transcript
```

Observed: `FAIL_HOLLOW`, exit 1. Candidate: `3 passed in 0.93s`.
Parent with candidate tests overlaid: `3 passed in 1.30s`.
`HEAD^` is `17f685cf8833083a6f552af64658337725154729`, which already contains
the already-advised exclusion. This comparison does not remove that exclusion.

Relevant implementation: `runtime_ac.author_issue_spec` translates the prose
break/revert criterion into a base comparison plus a manual mutation obligation;
`runtime_ac_gate.recheck_merged` sets its base to `HEAD^`. Thus `hollow_break`
here records a completed but green base comparison, not a command-launch failure.
No gate, spec, issue criteria, or runtime acceptance result was overridden.

Remaining unverified: the automated gate is still UNRUNNABLE/hollow_break; the
literal second-run mutation criterion and the production live sample-size/
agreement criterion are not complete. The original 2026-11-08 deadline has passed.

## Continuation check — 2026-11-11 America/Chicago (2026-11-12 UTC)

Outcome required on real input: `adjudicator_live.py --line` prints advised A > 0,
G >= 20, agreement against 0.75, and a qualification decision consumed by #575.
Command: `/opt/anaconda3/bin/python3 src/adjudicator_live.py --line`.
Stop rule: two repair attempts per demonstrated failure or 60 minutes. The first
broken production step remains the missing newly certified, Brain-persisted live
observation after owner sync; no paid dispatch or production write was launched.

### Branch continuity and demonstrated CI repair

Started at `00cec79b8ae2892cecc04d113c7eb4d83c22f812` on `orch/issue-643`.
`git worktree list --porcelain` showed this sole holder for the branch, clean.
Inspected preserved opener ref
`preserved/stranske__Orchestrator__643__opener-20261111T190307Z` (`c2387898`).
Its paths and content were retained untouched. The useful retirement idea was
carried into the small policy/consumer connection below; its broader collector,
event-recovery and UUID changes were not transplanted. Deterministic advice event
identity is unchanged. No holder was reset or cleaned.

`gh pr checks 1356 --repo stranske/Orchestrator` identified failed lint-format and
Gate. Read job logs 130914335473 and 130916143479 of run 43136851049. Black printed
`would reformat .../tests/test_adjudicator_retro.py` and `1 file would be
reformatted, 706 files would be left unchanged`; Gate printed
`Python CI result: failure`. Applied Black only to changed files. The focused
Black check now prints `3 files would be left unchanged`. No pins, workflow
infrastructure, gates, review floors, or existing implementation were replaced.

### Fixture and behavioral mutation evidence

At starting head, the exact three named pytest nodes above printed
`6 passed in 5.28s` (four parameter combinations in the first node).

In an isolated `git archive HEAD`, changed only the unique
`seen_targets = live_advised_targets(db)` to `seen_targets = set()`.
The first named test reached the report-free replay and printed, in all four
variants:

```text
AssertionError: the second run must not dispatch or write
assert (2, ['executi...adow_advice']) == (1, [])
At index 0 diff: 2 != 1
4 failed in 3.02s
```

Restored the exact original bytes and reran: `4 passed in 5.12s`.
This supersedes the earlier selection-only mutation evidence: replay now reaches
and detects the second backend dispatch AND event-write attempt. It is still
not the literal two-event-row requirement: deterministic event identity can
replace one stored row. No event identity was altered to manufacture two rows.
That literal acceptance requirement remains unresolved.

### Missing policy connection repaired; fixture retirement only

The current branch had neither a 20-grade/0.75 decision in the weekly line nor
a live dispatch consumer of that decision. Added `capabilities.adjudicator_live_policy`:
fewer than 20 grades is `insufficient_sample`; agreement below 0.75 at n >= 20
is `retired`; otherwise `qualified`. Reconciliation retains a sticky live-role
retirement receipt, and the live runner checks it before certification or paid
dispatch. Weekly output names agreement, decision and sample shortfall.
Blocking/drainable pair: dispatch stops while retired or switched off;
`reconcile_live` and `weekly_line` continue later-truth grading. This retires the
live shadow dispatch path, not all callers of the generic adjudicator role or
the global capabilities registry. Broader role retirement is unverified.

Focused tests use 20 distinct private-Brain disputed runs with actual durability
rows: 14/20 retires and stops dispatch, 15/20 qualifies, 19 grades cannot retire,
and later truth corrections still reconcile after sticky retirement. These are
fixtures, not production samples. The changed-file suite printed
`82 passed in 10.26s`. No full suite was run.

### Production readback and scheduling boundary

Real `--line` output with the changed reader:

```text
live: disputes 495, advised 4, uncertified 61, graded 4 (agree 2, disagree 2); uncertified reasons: other 40, prompt_too_large 8, criteria_shape 7, no_criteria 6; agreement 0.500 against 0.75; qualification insufficient_sample; target 20 by 2026-11-08 MISSED; blocking=dispatch retired or switched off; drainable=durability grading
```

Ran `reconcile_live` against a temporary copy of the production report, reading
the actual Brain without modifying it. Actual later-truth result: A=4, G=4,
agree=2, disagree=2, merge-rule agree=4. Agreement 0.500 is below 0.75 but the
pool is too short to retire or qualify. Report advice is not proof of new Brain
persistence: a read-only aggregate found zero current `live_shadow_advice`
events. Thus the new persisted-event criterion is NOT met by these four legacy
report entries. No newly certified dispute or production idle rerun was observed.

The launchd plist `com.stranske.orchestrator` names
`/Users/teacher/.codex/orchestrator-mirror/orchestrate.sh --active`, minute 40.
Both branch and deployed script contain daily `adjudicator_live.py` after the
durability sweep (lines 1087–1093), with the existing `--enabled` check, search
gate and bounded limit. They contain the weekly `switch_review.py` invocation
(line 839); its report reader consumes `adjudicator_live.weekly_line` and its
text writer includes the line. This verifies configured invocation and code
consumption, not a fresh loaded scheduler execution or successful owner sync.
Owner sync and observing the next authentic dispute remain undone. No mirror,
launchd setting, switch, production report, or Brain record was changed here.

The 2026-11-08 historical deadline was missed and cannot be retroactively met.
G >= 20, production persisted advice and duplicate-free idle rerun, real
underperforming-role retirement, and live M2 qualification remain unmet.
PR checks/review and merge require fresh exact-head readback after this push;
this receipt does not claim them green or the PR merged.

### Exact-head follow-through and CI consumer assertion repair

Committed/pushed `d49be919b29d0d4aee9c43d9bebd1783b896588d` on the existing
branch. The exact three named nodes printed `6 passed in 5.50s` at that head.
A fresh isolated archive of that exact head printed `4 failed in 3.13s` on the
exclusion mutation (second dispatch count 2 versus 1 and advice write attempted),
then `4 passed in 5.25s` after byte-identical restoration.

Fresh CI lint-format passed. Flat exec-mirror shard 6/6 of run 43144993113 then
printed two failures in `tests/test_adjudicator_live.py`: the pre-existing
later-truth and weekly-consumer assertions expected the line without the newly
required agreement/qualification suffix. Preserved their original count/prefix
assertions and added explicit checks of agreement 1.000, threshold 0.75,
insufficient sample, and continued grading drain. No production code changed in
this follow-up. Focused command:

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_adjudicator_live.py tests/test_adjudicator_retro.py
```

Observed: `112 passed in 12.14s`. This is a changed-file run, not the full suite.
Final CI and seven-minute exact-head review readback occur after the follow-up
push and must be reported separately; neither production qualification nor merge
is implied by these local results.

### 2026-11-12: acceptance reconciliation and real-dispatch isolation

Reviewed the recent commits through `27d21624` and ran the three named acceptance
tests: **10 passed** (eight decision/truth/merge-rule combinations and two refusal/isolation
tests). The named lifecycle test now checks every non-event Brain table after real role
dispatch, allowing one new role run while requiring all existing runs to remain unchanged.
A temporary mutation that changed the disputed run's task type failed at
`existing runs are unchanged`; the mutation was reverted. The three adjudicator modules
(`test_adjudicator_retro.py`, `test_adjudicator_live.py`, and
`test_adjudicator_live_log_bounds.py`) then reported **119 passed**, with `-m "not slow"`.

Verified acceptance checklist reconciliation:

- [x] `test_live_shadow_adjudicates_a_new_dispute_once_and_grades_on_truth`
- [x] `test_live_shadow_never_adjudicates_an_uncertified_case`
- [x] `test_live_shadow_writes_no_merge_input`
- [ ] Production pass number: still requires the owner's sync and observed live receipts.

The PR-body update was attempted, but the GitHub connector returned
`MCP tool call requires approval, but approval policy is never`. These three verified
checkboxes still need to be applied to PR #1356. No production receipt or deadline extension
is claimed; the original 2026-11-08 sample deadline has passed.

Staging the verified change also failed: `.git` is mounted read-only, so `git add`
could not create `.git/index.lock`. The test and this note remain uncommitted in the
checked-out PR branch; no branch, PR, or production state was created.

Black 26.10.1 formatted the changed test without changes. Its full-repository check
(`--check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .`) passed:
**699 files would be left unchanged**. Tools were loaded from official upstream sources
under `/tmp` because pytest/Black were absent and PyPI DNS was unavailable. The full Black
check used one worker and a temporary periodic asyncio wakeup to avoid stalled completion
notifications in this sandbox; the formatter and checks were otherwise unmodified.

### 2026-11-12: independent certification refusals and existing merge lineage

Reviewed recent commits through `8572e9dc` and revalidated the named acceptance tests
before changing their assertions: **10 passed** with `-m "not slow"`. The existing
uncertified fixture changed both the head and the body, so it did not independently
exercise target/head provenance checks. Split it into four refusal cases: wrong
head, wrong PR number, missing criteria, and missing body. Each now verifies the
persisted reason and count, unchanged Brain tables, no advice or dispatch, and no
second collection on the next daily run.

The merge-input isolation test now starts with a verifier completion event and an
accepted verifier influence edge. It verifies that shadow advice preserves the
existing event, the run's latest verification event, every non-event Brain table,
and both persisted verdicts. This exercises preservation of populated lineage
rather than only empty tables.

Validation:

- The three named acceptance nodes: **13 passed**.
- `pytest -q tests/test_adjudicator_retro.py tests/test_adjudicator_live.py
  tests/test_adjudicator_live_log_bounds.py tests/test_completion_events.py
  tests/test_capabilities.py -m "not slow" -rs`: **178 passed, 2 skipped**. The
  existing environment checks skip because the machine-local capability ledger
  is unavailable on this read-only host; no tests were disabled or changed to skip.

The verified checklist remains:

- [x] Named live-dispute lifecycle and later-truth grading test.
- [x] Named uncertified-case refusal test.
- [x] Named merge-input isolation test.
- [ ] Production pass number and live qualification sample.

The PR-body reconciliation, `needs-human` label, and blocker comment were each
attempted through the GitHub connector and each rejected with
`MCP tool call requires approval, but approval policy is never`. The three verified
PR checkboxes therefore still need to be applied externally. This does not claim
owner sync, production receipts, or satisfaction of the elapsed 2026-11-08 deadline.

Staging the two changed files failed again: `git add` could not create
`.git/index.lock` because `.git` is mounted read-only. No commit or push was
possible; the test changes and this receipt remain in the existing worktree.
The PR was read back as open and ready for review (`draft=false`).

Black 26.10.1 formatted the changed test. The full-repository check with
`--check --line-length 100 --exclude '(\.workflows-lib|node_modules)' .` passed:
**699 files would be left unchanged**. As in the preceding receipt, tools were
loaded from official upstream repositories into `/tmp` because pytest/Black are
absent and package-server DNS is unavailable. The first check used a periodic
asyncio wakeup to keep completion notifications moving; formatting behavior was
unchanged.
