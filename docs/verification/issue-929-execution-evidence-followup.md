# Issue 929 execution evidence follow-up

This continuation advances the first task locally. Check-run collection requests
`filter=all` and rejects missing or mismatched `head_sha` in both list responses
and failure-detail responses. The named regression distinguishes the evaluated
revision from the current PR head and includes missing/wrong revision controls.
The module selftest fixture now supplies the API's `head_sha` field.

Verified on this working tree:

- `python3 src/adjudicator_live.py --selftest` passed with private temporary state.
- Three independent stdlib checks passed: qualified evidence across two pages
  includes both conclusions and a bounded failure log; a wrong list revision is
  rejected; a wrong detail revision is rejected.
- `python3 -m py_compile src/adjudicator_live.py tests/test_adjudicator_live.py`
  and `git diff --check` passed.

The independent checks do not establish that the required named pytest test or
its deliberate-break control passed. No task checkboxes were completed here.

Pytest and Black are missing. Installing them into `/tmp/adjudicator-venv`
failed because the runner could not resolve PyPI. The required repository-wide
Black command exited 127. The explicit pre-commit gate therefore prevents a
commit; the source and test changes remain uncommitted.

Required follow-up: provide pytest and Black, format the changed files at line
length 100, run the focused pytest suite with `-m "not slow"`, run the named
deliberate-break/restore check, and pass the required repository-wide Black
check before committing. No protected-file changes are required.

PR #939 was verified open with `draft=false`. Attempts to add its blocker
comment and `needs-human` label were rejected because the GitHub tools require
approval and the current run's approval policy is `never`.
