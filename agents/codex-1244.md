<!-- bootstrap for codex on issue #1244 run:42499436138-1 -->

The requested read-only Brain connections, guarded assignment backfill, and named
write-lock regression test are already implemented. This round extends
`test_summary_line_reads_while_brain_write_locked` to cover both `NULL` and
already-assigned rows under DELETE and WAL journaling, and verifies reporting
does not change the assignment.

Validation: the four triage cases and two runtime-AC write-lock cases passed when
their unchanged function bodies were copied into a temporary standard-library
harness with isolated Brain paths. Temporarily restoring `feedback._conn()` in
the triage reader caused the named test's cycles assertion to fail with
`UNKNOWN — outcome evidence unreadable (OperationalError)`. Restoring the
read-only connection made all six cases pass again. `git diff --check` passes.

The runner lacks pytest and Black; installation failed because PyPI DNS is
unavailable. The required pytest invocation and Black formatting/check commands
could not run, so this change remains uncommitted and task checkboxes were not
updated. Rerun those checks before committing. GitHub API access also failed,
preventing a PR comment, label update, or readiness verification.

Follow-up verification and reconciliation:

- Both report readers still use explicitly closed `mode=ro` connections, and
  `_migrate_schema` still guards assignment backfill with a NULL-row query.
- The named triage regression now also covers absent committed outcomes while
  the writer inserts an uncommitted pending outcome. It must report `n/a (0
  judged)`, keeping readable-but-empty evidence distinct from `UNKNOWN`.
- A temporary standard-library harness imported the actual test modules using
  identity fixture/parameter decorators and called their unchanged test bodies.
  All eight triage cases, two runtime-AC cases, and the assignment migration test
  passed. This is not a pytest run and does not exercise pytest's suite fixtures.
- Temporarily restoring `feedback._conn()` made the named triage assertion fail
  with `UNKNOWN — outcome evidence unreadable (OperationalError)`. The read-only
  source was restored in a `finally` block, and the harness passed again.
- Every task and acceptance checkbox, including duplicates, has implementation
  and behavioral evidence to be checked. The PR-body update was blocked because
  the GitHub tool requires approval and this run's approval policy is `never`.
- Pytest and Black remain unavailable across the installed Python interpreters;
  installing them failed because PyPI DNS resolution is unavailable. The requested
  pytest, Black format, and repository-wide Black check commands could not run.
  The Python test change is deliberately uncommitted until the mandatory Black
  gate can pass. PR #1245 was verified open and ready for review (`draft=false`).
- The requested blocker comment and `needs-human` label were also blocked by the
  same GitHub approval requirement. A reconciled PR body is staged as a temporary
  file at `/tmp/pr-1245-reconciled.md`; no remote metadata changed.
