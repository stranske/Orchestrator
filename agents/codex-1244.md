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
