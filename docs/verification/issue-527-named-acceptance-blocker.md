# Named cost acceptance checks: runner blocker

This run strengthens the two named tests in `tests/test_adjudicator_retro_cost.py`.
The measured-usage test checks the exact arguments passed to `record_role_run`
and their persisted recovery receipt, including zero counts. The missing-usage
test also removes the eligible pool and refreshes the saved report, checking
that its estimate becomes unmeasured without another backend dispatch or null
display text.

These edits are uncommitted and unverified by pytest. Neither pytest nor Black
is installed on this runner. Installing both into an isolated `/tmp` virtual
environment failed because `pypi.org` could not resolve; no cached installation
was found. Python compilation, the adjudicator module's nine-check selftest,
and `git diff --check` pass, but do not satisfy the acceptance criteria.

- [ ] Both named tests pass with `-m "not slow"`.
- [ ] Drop the token fields from the role record call; the measured-usage test fails; restore.
- [ ] Print null for absent cost; the missing-usage test fails; restore.
- [ ] Run Black at line length 100 and pass the required repository-wide check before committing.

Supply pytest and Black or restore permitted package access to resume these
checks. No deliberate source break was made without a runnable pytest baseline.
Attempts to add a `needs-human` label and blocker comment to PR #545 were
rejected because those connector actions require approval and this run's
approval policy is `never`. The PR was confirmed open and ready for review.
