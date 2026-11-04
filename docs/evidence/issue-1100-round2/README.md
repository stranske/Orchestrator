# Issue #1100: second observation and CLI checks

Operational acceptance remains **unproved**. Production inputs are unavailable on this runner;
their counts are unknown, not zero. The new tests use private synthetic state and are not host
receipts. No production receiver, hook, or lock was changed.

## Task reconciliation

Reviewed commits `8970ad6` and `efaa78c`. The latter added the source-review record and three
passing CLI tests. Only the review task, appearing twice in the PR body, is complete:

- [x] Read the complete merged rate_incidents.py, legacy matcher and focused tests to finish the review omitted from this input.
- [ ] Correlate existing production decisions, completed exits, persisted rounds, and accepted edges.
- [ ] Report cumulative and newly attributable production joins for the same window.
- [ ] Obtain fresh production switch-review text output for that window.
- [ ] Obtain passing repository tests and the requested verifier terminal result.
- [ ] Adjudicate the original deadline from attributable production timestamps.

All acceptance criteria remain unchecked. The reconciled PR body is prepared at
`/tmp/issue1100-reconciled-pr-body.md`; both occurrences of the review checkbox are checked.
Publishing the PR-body update, blocker comment, and `needs-human` label each returned:

```text
MCP tool call requires approval, but approval policy is never
```

The remote PR body therefore still needs reconciliation. Read-only GitHub inspection confirmed
PR #1101 is open and ready for review (`draft=false`).

## Changes and validation

Added three tests to `tests/test_relay_acceptance_cli.js`:

1. Three interleaved decisions complete in a different order. Each accepted edge retains the
   exact raw decision ID, decision timestamp, exit timestamp, and persisted round ID; the third
   decision stays pending without credit.
2. A decision before the deadline completes one second afterward. Reading the deadline window
   after persistence reports acted_on=1 and outcome=0 despite cumulative joined=1. The later
   window reports outcome=1. Repeating the earlier read leaves Brain and pacing-log bytes intact.
3. Decisions one second before deployment and exactly at deployment both complete afterward.
   Cumulative joined=2, while the deployment window reports acted_on=1 and outcome=1.

The original three tests passed before editing. All six tests passed after editing:
`node --test --test-isolation=none tests/test_relay_acceptance_cli.js`
([terminal result](relay-cli-tests.txt)). Node syntax and `git diff --check` also pass.
No Python files changed, so the Python-only pre-commit Black gate does not apply.

The repository pytest attempt and bounded verifier attempts retain their commands, terminal
output, exit codes, elapsed times, and execution bounds in the adjacent JSON/text files.

- Repository pytest with `-m "not slow"`: exit 1, pytest absent
  ([terminal output](repository-pytest.txt), [execution record](repository-pytest.json)).
- `/opt/anaconda3/bin/python3 src/verify.py`: exit 127, interpreter absent
  ([terminal output](requested-verify.txt), [execution record](requested-verify.json)).
- Available-interpreter `src/verify.py`: exit 124 after 120.012 seconds, no verifier output;
  the entire child process group was terminated at the execution bound
  ([terminal output](available-verify.txt), [execution record](available-verify.json)).

Neither repository tests nor the verifier have a passing terminal result. The fallback verifier
inherited `PYTEST_ADDOPTS=-m "not slow"`; no floor-update or test-disabling flag was used.

## Observation limits and floor identity

[Environment readback](environment.json) confirms absent production pacing/exit logs and Brain,
with no configured path overrides. This blocks correlation, same-window production counts, and
a production switch-review artifact. An empty runner report would not satisfy those tasks;
no speculative report-generation repair was made.

At this observation the deadline **2026-11-04T08:16:32Z** is still in the future. Missing host
access cannot establish either timely acceptance or a missed deadline. The deadline regression
test proves window semantics only; it does not adjudicate the real deployment.

[Floor comparison](source-and-floor.json) confirms byte equality with assessed local
`origin/main` `e86313cb6f1f6f047896b5c1bbb5d9c3c68d339e`. A fresh read through the GitHub
connector also returned identical bytes for main's floor blob
`3ac9a5d4ce7b5854ae9cf8b8a7a2d23a4ba0a7f9`. SHA-256 for all three copies:
`780186ee0caf07002daac988ecbd91c4d58efb8e1957adb164c8ebae25d0f849`.
The local remote-tracking ref was not advanced.

## Commit blocker

After validation, `git add tests/test_relay_acceptance_cli.js docs/evidence/issue-1100-round2`
returned exit 128:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

The source change and evidence remain in the working tree. No commit or push was possible.
Remaining work requires production read access, the requested interpreter/test tools, writable
Git metadata, and an approved GitHub mutation channel. Review completion does not imply
operational acceptance.
