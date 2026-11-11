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
