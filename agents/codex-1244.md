<!-- bootstrap for codex on issue #1244 run:42499436138-1 -->

The existing implementation remains intact: triage and runtime-AC reports use
explicitly closed read-only SQLite connections, assignment backfill only writes
when NULL assignments exist, and the regression covers committed snapshots,
assignment preservation, write contention, and genuine read failures.

Native validation on 2026-11-08 used `/opt/anaconda3/bin/python3`; this checkout
has no selected `.venv`, `venv`, or `.python-version`.

- `python -m pytest -q tests/test_triage_shadow.py::test_summary_line_reads_while_brain_write_locked`
  printed `8 passed in 0.60s` with the actual repository fixtures.
- Deliberate break: replaced only the read-only connection context in
  `src/triage_shadow.py::summary` (called by `summary_line`) with
  `with feedback._conn() as conn:`. The same pytest command printed
  `8 failed in 46.92s`; all eight failures were the cycles assertion at
  `tests/test_triage_shadow.py:425`, reporting
  `UNKNOWN — outcome evidence unreadable (OperationalError)`.
- A `finally` block restored the original source bytes. The evidence wrapper's
  subsequent assertion used the wrong assertion-text spelling and failed; an
  independent read of the captured pytest output verified all eight expected
  failures. The restored named pytest command printed `8 passed in 0.51s`.
- `python -m pytest -q tests/test_triage_shadow.py tests/test_runtime_ac_shadow.py tests/test_feedback_registry_queries.py`
  printed `183 passed in 82.17s (0:01:22)`.
- `python -m black --check --line-length 100 src/triage_shadow.py src/feedback.py tests/test_triage_shadow.py tests/test_runtime_ac_shadow.py tests/test_feedback_registry_queries.py`
  printed `5 files would be left unchanged`.
- `python -m black --check --line-length 100 .` printed
  `1014 files would be left unchanged`.
- An initial focused Black invocation named nonexistent
  `src/runtime_ac_shadow.py` and exited 2; the corrected invocation above checks
  all five Python files changed by this PR. No dependency pins changed.

Reconciliation before this evidence update: the working tree was clean and
local HEAD and PR #1245 both matched
`b30ce2262ff0846575fb60ec296b3a179f65dcb6` on `codex/issue-1244`.
`git worktree list --porcelain` and the preserved-holder search found no second
holder for this branch. Existing source and tests were already committed;
prior claims that they remained uncommitted are stale.

PR #1245 was OPEN and ready (`isDraft=false`), with no review threads across
the complete GraphQL result. Existing Gate run 42535385729 failed in Python
3.13/3.14 shard 2/6: xdist reported zero items and pytest exit 5 (workers exit 4),
not a failure in the affected tests. The exact-head merge snapshot refused
`PR is not cleanly mergeable` while Gate was red. Delivery remains pending a
fresh post-push check/review/floor read and the authorized merge guard; this
validation evidence does not claim a merge or a green Gate.
