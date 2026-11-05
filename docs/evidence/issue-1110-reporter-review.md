# Issue #1110: reporter review

- [x] Read the complete merged reporter and CLI tests before relying on their counts; verify snapshot consistency, eligibility boundaries, exact-ID matching, ambiguous-exit handling and read-only behavior.

Reviewed the complete `scripts/report_relay_join_window.js` and
`tests/test_relay_acceptance_cli.js`, including their SQL selection and all 11 original
CLI tests, against the relay reader and importer in `src/feedback.py`. This completes
the reporter review only. All production acceptance criteria remain unchecked.

The review found a false positive: an accepted edge could count as an attributable
join even when its outcome payload named a different lane or chosen agent from its
source decision and matching round. Two private-fixture regression cases failed
before the fix (`accepted_window_joins` was 1 rather than 0). The reporter now
requires both outcome fields to match. Both cases pass after the fix, and restoring
the original outcome restores its accepted join. No production importer was changed.

Validation on Node v24.22.1:

```text
node --test --test-isolation=none tests/test_relay_acceptance_cli.js
tests 17; pass 17; fail 0; skipped 0; exit 0
node --check scripts/report_relay_join_window.js
node --check tests/test_relay_acceptance_cli.js
git diff --check
```

The original cases verify pre-deployment and late exits, ambiguous duplicate exits,
unfinished decisions, missing accepted joins despite positive totals, and unavailable
state without database creation. The added cases verify both inclusive endpoints,
adjacent exclusions, a one-second window, case-sensitive literal IDs containing
punctuation, and refusal to match an ID suffix. A separate WAL connection commits
an outcome change between the reporter's first and subsequent reads: the current
report retains the earlier consistent snapshot, and the next report observes the
change. Repeated reporting preserves main-database, WAL and pacing-log bytes. These
are synthetic correctness witnesses, not production evidence.

At 2026-11-05T01:06:45Z the production pacing log, exit log and Brain were absent
from their default runner paths; no `HANDOFF_DIR`, `HANDOFF_PACING_LOG`,
`ORCH_LOCAL_RUNTIME` or `ORCH_FEEDBACK_DB` override was set. The requested
`/opt/anaconda3/bin/python3` was absent and the available `python3` had no pytest.
There is no production deployment receipt, raw-log reconciliation, Brain snapshot,
or fresh production switch-review artifact from this run. There is no passing
repository-wide test/verifier result, and the four reported preflight failures
have not been reproduced on the assessed baseline or classified as pre-existing.
`python3 -m pytest -m 'not slow'` terminates with exit 1 (`No module named pytest`);
`/opt/anaconda3/bin/python3 src/verify.py` terminates with exit 127 (interpreter
absent). These failures do not establish the status of the production baseline.

The assessed local `origin/main` is
`27ed52260d05b7f3178c1e3393ac90a42e742f66`, also the PR base returned by GitHub.
The working `.verify-floor.json` is byte-identical to that revision's file; SHA-256
is `00d01689a9b9f6604f94ffc8f48ac723c32142c159edcf616d846739c0b655ec`.
The floor was not edited. No Python files changed.

The original deadline, **2026-11-04T08:16:32Z**, has elapsed. Attribution remains
**unresolved** without production timestamps; this runner cannot establish timely
success or a missed production deadline. Any attributable success after that
timestamp must be recorded as late recovery. Completing the remaining tasks
requires the production seat and passing terminal validation without weakening
checks or changing the floor.

Delivery is blocked: `git add` returned exit 128 because `.git/index.lock` cannot
be created on the read-only filesystem. The GitHub source-commit attempt,
`needs-human` label and blocker-comment publication were each rejected:
`MCP tool call requires approval, but approval policy is never`. No commit, push,
label, comment or remote checkbox update was applied. The code, tests and this
review record remain in the working tree for an authorized write channel.
GitHub reported PR #1132 open with `draft=false`; it was not converted or closed.
