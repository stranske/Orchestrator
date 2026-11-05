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

## Baseline preflight follow-up, 2026-11-05

Reviewed `84051b3` before continuing. Its reporter fix and regression cases support
the completed reporter-review task; none of the production acceptance criteria
can be checked off. The duplicate review checkbox in the PR's full issue text
still needs alignment. The connector rejected that update because it requires
approval and the run's approval policy is `never`.

The production observation task remains blocked by unavailable host records.
For the next independent validation task, exported `src/`, `tests/`,
`pyproject.toml` and `.verify-floor.json` directly from assessed `origin/main`
`27ed52260d05b7f3178c1e3393ac90a42e742f66` into
`/tmp/issue1110-assessed-main` using `git archive`. Ran:

```text
node --test --test-isolation=none /tmp/issue1110-assessed-main/tests/test_value_chain_preflight_cli.js
tests 12; pass 8; fail 4; skipped 0; exit 1
```

[Raw baseline output](issue-1110-preflight-baseline.log) confirms exactly the
four previously reported failures. The two successful-design tests omit
`consumer_acts` from the expected check roster; the missing-consumer and
all-missing-declarations tests omit it from the expected failures. The baseline
already enforces this tenth requirement. These test expectations and the
admission implementation were byte-identical between the assessed baseline
and the starting PR head.

Updated the CLI tests to require the tenth check and both missing-consumer
failures. Added a case requiring a nonempty but unresolved consumer to fail
`consumer_acts` while still passing `outcome_path`, retaining all unverified
implementation obligations and checking read-only behavior. No admission
implementation or verification threshold was changed.

```text
node --test --test-isolation=none tests/test_value_chain_preflight_cli.js tests/test_relay_acceptance_cli.js
tests 30; pass 30; fail 0; skipped 0; exit 0
node --check tests/test_value_chain_preflight_cli.js
git diff --check
```

[Raw combined output](issue-1110-preflight-relay-green.log) is focused validation,
not a passing repository-wide result. `python3 -m pytest -m 'not slow'` still
exits 1 with `No module named pytest`. No Python files changed. The floor remains
byte-identical to the assessed baseline with the SHA-256 recorded above.
The validation task remains unchecked until repository tests and `src/verify.py`
terminate successfully. Production timestamps and deadline attribution remain
unresolved; any success after `2026-11-04T08:16:32Z` is late recovery.

At `2026-11-05T01:21:14Z`, staging the changed test returned exit 128 because
`.git/index.lock` could not be created on the read-only filesystem. No commit or
push was possible through the local Git channel. The changes remain available
in the working tree.
The follow-up `needs-human` label and blocker comment were also rejected by the
connector with `MCP tool call requires approval, but approval policy is never`;
neither was published.

## Raw-log reconciliation follow-up, 2026-11-05

Added optional `--pacing-log /path/to/retained/relay-pacing.log` to the existing
reporter. Top-level counts still describe persisted explicit decisions. The
additional `raw_reconciliation` population includes validated explicit raw
decisions absent from Brain, with exact IDs, raw facts, decision timestamps,
persisted round IDs and exit timestamps, accepted edge IDs, and the input log's
SHA-256. Missing persisted decisions and disagreements with persisted facts are
reported explicitly. Conflicting reuse of an ID cannot earn an accepted join;
identical repeated records are deduplicated. Missing requested logs fail closed.

The input must be a retained log captured with the Brain snapshot. Reading two
live files does not make them atomic. This comparison excludes legacy and invalid
lines (with an excluded-line count), and uses persisted rounds rather than raw
exit logs. It does not verify deployment provenance or establish production
acceptance. No additional task checkbox is complete.

Four added fixture cases failed against the previous implementation. A fifth
case was subsequently added for parser validation. After implementation,
validation passed:

```text
node --test --test-isolation=none tests/test_relay_acceptance_cli.js tests/test_value_chain_preflight_cli.js
tests 35; pass 35; fail 0; skipped 0; exit 0
node --check scripts/report_relay_join_window.js
node --check tests/test_relay_acceptance_cli.js
git diff --check
```

The WAL snapshot/read-only test now also exercises raw reconciliation. Production
pacing/exit logs and Brain remain absent at the resolved runner paths. Pytest and
`/opt/anaconda3/bin/python3` remain unavailable, so this is not a passing
repository-wide test/verifier result. No Python files changed.

The currently assessed local `origin/main` is
`d227431f8bf6fff1105395445c52c3505432b766`. Unlike the earlier assessed revision,
its floor is **not** byte-identical to the working floor. Its SHA-256 is
`e75503f1365cbb2cdd45e4c6269b678e809b3063d0dd8922b6c49f53acba9214`;
the working SHA-256 remains
`00d01689a9b9f6604f94ffc8f48ac723c32142c159edcf616d846739c0b655ec`.
The floor was not changed. Production attribution remains unresolved against
the original `2026-11-04T08:16:32Z` deadline; any later success is late recovery.
GitHub reports PR #1132 open and ready (`draft=false`).

Delivery was attempted after validation. `git add` failed with exit 128 because
`.git/index.lock` cannot be created on the read-only filesystem. The connector
rejected remote tree creation, `needs-human` labeling, and the blocker comment
with `MCP tool call requires approval, but approval policy is never`. No commit,
push, label, or comment was applied; source and tests remain in the working tree.

## Raw exit-log comparison follow-up, 2026-11-05

Reviewed the three preceding keepalive commits. Both reporter-review checkboxes
in PR #1132 are already checked; no additional task is verified complete.

The reporter now accepts `--exit-log` alongside `--pacing-log`:

```text
node scripts/report_relay_join_window.js --db /retained/orchestrator.db --start 1793693792 --end END_UNIX_SECONDS --pacing-log /retained/relay-pacing.log --exit-log /retained/relay-exits.log
```

The optional comparison reads the documented `RELAY exit=` trailer format,
retains raw lines, line numbers, exact IDs and exit facts, and hashes the input.
It exposes eligible raw exits with missing persisted rounds, unmatched exit IDs,
and disagreements with persisted timestamps, agent, lane or status. Duplicate
trailers remain ambiguous, including byte-identical repeats. Attribution requires
exact second-level agreement between trailer and round timestamps; mismatches
remain visible for adjudication. A missing requested log fails closed.
`completed_exit_source` distinguishes raw exits from the persisted-round-only
comparison. Top-level counts still describe persisted evidence.

All four new regression cases failed before implementation. After implementation,
the combined relay/preflight command above passes **39 tests**, exit 0; Node
syntax checks and `git diff --check` pass. The WAL snapshot test now includes
exit-log comparison and verifies all three retained inputs preserve their bytes.
These tests establish tool behavior only. Deployment provenance, consistent
capture and positive production switch-review counts still require the host
records requested from the operator; no operational checkbox is completed.

Assessed local `origin/main` is `904dd78fb972e1c986df25595222866a9788538d`.
Its floor SHA-256 is `e75503f1365cbb2cdd45e4c6269b678e809b3063d0dd8922b6c49f53acba9214`;
the working floor still has SHA-256
`00d01689a9b9f6604f94ffc8f48ac723c32142c159edcf616d846739c0b655ec`.
Byte equality is false, and the floor was not edited. Deadline attribution remains
unresolved; any success after `2026-11-04T08:16:32Z` must be late recovery.

Delivery remains blocked: staging failed because `.git/index.lock` is on a
read-only filesystem. The GitHub connector rejected tree creation, `needs-human`
labeling and the blocker comment with `MCP tool call requires approval, but
approval policy is never`. No new commit or remote update was applied. Final
readback confirms PR #1132 is open with `draft=false` at `2a255d2`.

## Ambiguous-exit omission counts, 2026-11-05

Fixed a reconciliation summary defect: duplicate completed exits correctly
prevented an accepted join, but also removed the decision from both the matching
completion and unaccounted-completion counts. Raw reconciliation now counts a
decision with any matching completed exit as an obligation, while retaining the
existing unique-attribution requirement for acceptance. Duplicate raw trailers
and duplicate persisted rounds remain explicitly ambiguous and earn no join.

The changed duplicate-trailer assertion and new positive-total regression both
failed before the fix. After the fix, the combined relay/preflight CLI suites
pass **40 tests**, exit 0. The regression covers identical and different duplicate
trailers, duplicate persisted rounds, a mismatched trailer preceding a matching
one, wholly mismatched agents, retained round IDs, and unchanged database/log
bytes. Node syntax checks and `git diff --check` pass. No Python files changed.

No additional task is complete. Production records remain unavailable, and pytest
is absent; installation in an isolated environment could not obtain pytest.
There is no passing repository-test/verifier result. Assessed local `origin/main`
is `b69ff474d3a87a271a7345874bcfc19b03a736bf`; its floor SHA-256 is
`d9640632d10dbebbf82c1de1a4623fcfc2fddfc1ab280533732bab3abb51e1ed`.
The unchanged working floor still hashes to
`00d01689a9b9f6604f94ffc8f48ac723c32142c159edcf616d846739c0b655ec`,
so byte equality is false. Deadline attribution remains unresolved without
production timestamps; success after `2026-11-04T08:16:32Z` is late recovery.
Local staging failed because `.git/index.lock` is on a read-only filesystem.
The GitHub connector also rejected tree creation, `needs-human` labeling and
the blocker comment with `MCP tool call requires approval, but approval policy
is never`. No commit, push, label or comment was applied.
