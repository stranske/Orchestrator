# Issue #1100: persisted decision window reporting

Production acceptance remains **unproved**. This round advances the same-window reporting
task with a read-only tool; its validation uses synthetic private state. No production
receiver, hook, or lock was changed.

## Reconciliation

Reviewed commits `8970ad6`, `efaa78c`, and `96cbd07`. Only the complete source/matcher/focused-test
review task is verified, as documented by the previous two rounds:

- [x] Read the complete merged rate_incidents.py, legacy matcher and focused tests to finish the review omitted from this input.
- [ ] Correlate existing production decisions, completed exits, persisted rounds, and accepted edges.
- [ ] Report cumulative and newly attributable production joins for the same window.
- [ ] Obtain fresh production switch-review text output for that window.
- [ ] Obtain passing repository tests and the requested verifier terminal result.
- [ ] Adjudicate the original deadline from attributable production timestamps.

Every operational acceptance criterion remains unchecked. A PR body with both occurrences
of the review checkbox checked is retained at `/tmp/issue1100-reconciled-pr-body.md`.
Publishing that update was rejected by the connector, so the remote body remains unchanged.

## Implementation and validation

Added `scripts/report_relay_join_window.js`, following the existing built-in Node SQLite
reporting convention. It opens an explicit Brain path read-only and reads one SQLite
transaction snapshot. It does not import decisions, replay hooks, or start report generation.
The caller supplies inclusive Unix-second start/end boundaries:

```text
node scripts/report_relay_join_window.js --db /path/to/orchestrator.db --start 1793693792 --end END_UNIX_SECONDS
```

The report separates current cumulative accepted source bindings for the relay capability/version
from persisted explicit decisions, matching completed-exit decisions, and accepted joins in the
selected window. It retains raw IDs, decision timestamps, round IDs, exit timestamps and statuses,
and accepted edge IDs. A binding currently present does not establish when it first appeared.
An exit after the window, duplicate explicit rounds, mismatched facts, or an unrelated outcome
cannot earn an attributable window join. Failed completed rounds can still provide outcomes;
the report does not treat success as an eligibility requirement.

This population is explicitly limited to decisions already persisted in Brain. It excludes
legacy adjacency and cannot detect raw-log decisions that have never reached Brain. It cannot
establish production provenance, raw-log completeness, fresh switch-review counts, or whole-task
acceptance. Missing/unreadable Brain yields unavailable status and null counts, not measured zero.
Node >=22.13 is required; execution here used Node 24.22.1.

Five additional tests run against private Brain state populated by real CLI calls. They verify:

1. Historical and later-completing bindings remain in the cumulative count while the window
   reports only its attributable joins; later recovery is separate.
2. Positive same-window monitor acted_on/outcome totals can coexist with a completed decision
   missing its accepted join. The report identifies that exact ID and round.
3. Duplicate explicit rounds remain ambiguous even when an accepted binding already exists.
4. Missing Brain remains unknown and no database file is created.
5. An accepted flag cannot substitute for matching round facts and relay-produced outcome evidence.

The tests also assert repeated reporting preserves fixture database bytes.
`node --test --test-isolation=none tests/test_relay_acceptance_cli.js`: **11 passed**, exit 0
([output](relay-cli-tests.txt), [bounded execution record](relay-cli-tests.json)).
Node syntax checks and `git diff --check` pass. No Python files were changed, so the Python-only
pre-commit Black requirement does not apply.

The required terminal attempts are retained:

- Repository pytest with `-m "not slow"`: exit 1, pytest is absent
  ([output](repository-pytest.txt), [record](repository-pytest.json)).
- `/opt/anaconda3/bin/python3 src/verify.py`: exit 127, the interpreter is absent
  ([output](requested-verify.txt), [record](requested-verify.json)).

Neither is a passing repository/verifier result. The available-interpreter verifier already
timed out in both previous rounds; no new verifier success is claimed.

## Operational evidence limits

[Environment readback](environment.json), observed at **2026-11-03T21:31:44Z**, confirms the
production Brain, pacing log, and exit log are absent, with no relevant path overrides set.
Production counts remain unknown. No empty-runner switch report is substituted for production
output, and no speculative report-generation repair was made.

The original deadline **2026-11-04T08:16:32Z** is still in the future at this observation.
Neither timely acceptance nor a missed deadline can be established here. Later observations
must retain attributable decision/completion timestamps and adjudicate the original window
separately from subsequent recovery.

[Floor comparison](source-and-floor.json) confirms byte equality with assessed `origin/main`
`e86313cb6f1f6f047896b5c1bbb5d9c3c68d339e` and freshly retrieved main floor blob
`3ac9a5d4ce7b5854ae9cf8b8a7a2d23a4ba0a7f9`. All copies have SHA-256
`780186ee0caf07002daac988ecbd91c4d58efb8e1957adb164c8ebae25d0f849`.
The floor file was not edited. [PR metadata](pr-state.json) confirms PR #1101 remains open
and ready for review (`isDraft=false`), and its base matches the assessed revision.

## Delivery blockers

Staging the source/test changes and evidence returned exit 128
([output](git-add.txt), [record](git-add.json)):

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

No commit or push was possible. Changes remain in the working tree.
The PR-body update, blocker-comment publication, and `needs-human` labeling were each rejected:

```text
MCP tool call requires approval, but approval policy is never
```

These remote updates were not applied. Completing acceptance requires production read access,
the requested interpreter/test tools, writable Git metadata, and an authorized GitHub mutation
channel. No additional task or acceptance checkbox is marked complete by these synthetic checks.
