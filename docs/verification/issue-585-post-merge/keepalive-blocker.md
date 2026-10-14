# PR 596 first-task follow-up, 2026-10-14

The local change to `src/runtime_ac_gate.py` fixes a reproduced mismatch with the
shared deliberate-break runner: its broken-candidate verdict is `FAIL_BROKEN`,
not `FAIL`. A failure now requires both that verdict and evidence of a failed
assertion in the candidate run. Hollow comparisons and tool errors remain
`UNRUNNABLE`. The authored spec and checkout commit remain unchanged.

`tests/test_runtime_ac_post_merge_break.py` adds four unittest cases, discoverable
by pytest. Three exercise the real shared runner and disposable Git fixtures;
the fourth checks six tool-error outputs. Before the fix the broken-merge case
failed with `UNRUNNABLE != FAIL`; all four cases pass after the fix.

Verified locally:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p test_runtime_ac_post_merge_break.py -v
  4 tests PASS, including six tool-error subcases
python3 src/runtime_ac.py --selftest
  PASS
python3 src/runtime_ac_gate.py --selftest
  PASS
PYTHONPATH=src python3 -m unittest discover -s tests -p test_keepalive_spec_retry.py -v
  1 test PASS
PYTHONPATH=src python3 -m unittest discover -s tests -p test_durability_feedback_recovery.py -v
  11 tests PASS
git diff --check
  PASS
```

Black and pytest are absent from every available Python installation checked.
Installing them failed because the restricted runner cannot resolve pypi.org.
The formatting command and full Black check could not run, and the targeted
pytest command could not run. No commit or push was made because the explicit
pre-commit Black gate has not passed. No checklist item is marked complete.

The attempts to post a blocker comment and add `needs-human` to PR 596 were both
rejected: the GitHub connector requires approval, while this run's approval
policy is `never`. A subsequent read confirmed PR 596 is open and ready for
review (`draft=false`) at unchanged head
`7e4e5ba4e023e1506c8b59ffc1400cccb9cf45a4`.

To finish: provide Black and pytest in the runner, format both changed Python
files, pass the prescribed repository-wide Black check, run the two runtime-AC
test files with `-m "not slow"`, measure the collection floor for the four new
cases without weakening any ceiling, and commit the implementation and tests.
The production M5b observation remains separately pending after owner sync.

## Subsequent keepalive round

The prior runner fix is already committed in this checkout. This round adds four
regressions to `tests/test_runtime_ac_post_merge_break.py`: real broken-candidate
delivery with one comment/reopen/label and an unchanged event on repeat; the same
failure with reopening disabled; a real hollow comparison commenting once as
`UNRUNNABLE`; and a failed label write retried without repeating execution,
commenting, or reopening. The fixtures use disposable Git repositories, private
SQLite state, the real shared runner, and a stubbed GitHub client.

All eight unittest cases pass. An in-process forced-PASS mutation makes the new
delivery regression fail at `PASS != FAIL`; the patch is then restored. Both
runtime module selftests and all eleven durability feedback recovery tests pass.

Black and pytest remain unavailable. Installing them into `/tmp` failed; the
format command, prescribed repository-wide Black check, and targeted pytest
command could not run. No commit/push or checklist completion was made. The
collection floor and all ceilings are unchanged pending actual pytest collection.
Posting the blocker comment and adding `needs-human` to PR 596 were again denied
because connector mutations require approval and this run's policy is `never`.
