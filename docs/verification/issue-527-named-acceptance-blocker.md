## Local closer resolution — 2026-10-12

The historical hosted-runner limitation below is resolved locally: both named tests pass with `-m "not slow"`; exact production copies dropping token fields or printing null each produce intended RED, then byte-identical restoration produces GREEN. Hashes/raw logs are in issue-527-coverage-review-20261012. All102 focused tests/23subtests and390-file Black check pass. No human package repair is needed; final hosted CI remains required.

# Named cost acceptance checks: runner blocker

This run strengthens the two named tests in `tests/test_adjudicator_retro_cost.py`.
The measured-usage test checks the exact arguments passed to `record_role_run`
and their persisted recovery receipt, including zero counts. The missing-usage
test also removes the eligible pool and refreshes the saved report, checking
that its estimate becomes unmeasured without another backend dispatch or null
display text.

Those Python test edits are committed in `416cc42` but remain unverified by
pytest. Neither pytest nor Black is installed on this runner. Installing both
into an isolated `/tmp` virtual environment failed because `pypi.org` could not
resolve; no cached installation
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

## Follow-up: runnable native-usage regressions

Reviewed recent commits `c6ad518`, `346b035`, `416cc42`, and `83c0b81` before
continuing. The eight implementation/documentation tasks are already checked;
neither remaining acceptance checkbox can honestly be checked on this runner.
Pytest and Black remain absent, a search of installed tools and cached wheels
found neither, and the PyPI retry failed at DNS resolution again. An attempted
PR-body reconciliation update was rejected because connector writes require
approval and this run's approval policy is `never`.

`tests/test_adjudicator_retro_cli.js` now adds seven runnable checks using Node's
existing test runner and the real Python dispatch/role/Brain/reporting path:
six native-usage fixtures (including zero and missing token directions) and one
missing-usage fixture that withdraws the eligible pool. They verify the exact
recording arguments, persisted recovery record, Brain metadata, imputation,
weekly/switch-review rendering, and refresh without another role run.

All seven targeted checks passed. Independent deliberate production mutations
were then applied one at a time and restored in `finally` blocks:

- Removing both token fields from the role record made all six transport checks fail.
- Printing `null` for absent measured cost made the missing-usage check fail.
- Printing `null` for absent estimated cost made the missing-usage check fail.

Both Python files were restored byte for byte. These Node checks supplement the
named pytest acceptance checks; they do not satisfy the requirement to execute
those exact pytest nodes. This follow-up commits only JavaScript test changes
and this note, so there are no Python file changes requiring the Black gate.

After restoring all mutations, the full command
`node --test --test-isolation=none --test-reporter=tap tests/test_adjudicator_retro_cli.js`
passed **61 tests, 0 failures, 0 skips**. `node --check` and `git diff --check`
also passed. The connector rejected the follow-up label and blocker comment
as well; PR #545 was re-read and confirmed open with `draft=false`.

The original checkout's `.git` directory is read-only: both staging and committing
failed to create `index.lock`. The verified changes were instead committed using
isolated Git metadata in `/tmp/adjudicator-cost-followup`, based on the same HEAD.
A mail patch is retained at `/tmp/adjudicator-cost-followup.patch` for application
to the original branch when its Git metadata is writable. The original working
files contain the changes, but its branch has not advanced and nothing was pushed.
