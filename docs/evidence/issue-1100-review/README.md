# Issue #1100: review and isolated CLI validation

Operational acceptance remains **unproved**. This runner cannot observe the production host.
The new tests are synthetic witnesses, not deployment receipts. No production receiver was
restarted, hook replayed, or lock released. No repository defect was demonstrated by this review.

## Task status

- [x] Read the complete merged `src/rate_incidents.py`, the matcher in
  `src/feedback.py:6423–6746`, and the focused tests in `test_rate_incidents.py`,
  `test_rate_incidents_expiry.py`, and `test_relay_pacing_feedback.py`.
  Verified the additional CLI witnesses described below. This also satisfies the duplicate
  review item under “Additional PR tasks.”
- [ ] Correlate existing production decisions, completed exits, rounds, and accepted edges.
- [ ] Report cumulative and newly attributable production joins for the same window.
- [ ] Obtain fresh production `switch_review.py --text-out` output for that window.
- [ ] Obtain passing repository tests and the requested verifier terminal result.
- [ ] Adjudicate the original deadline from attributable production timestamps.

## Review findings and executable checks

Explicit IDs persist in `routing_decisions_v2.causal_context_json`. The importer requires a
unique matching round, agent/lane agreement, and a decision timestamp no later than the exit.
Explicit decisions cannot fall back to adjacency. Legacy decisions require a single decision
strictly between unique lane exits; explicit exits still advance that boundary. Pre-cutoff
orphans retire without outcome credit, while pending explicit decisions survive log rotation.
The hook persists the round before importing pacing data and retains authoritative provider
incidents when the importer raises a handled error.

`ingest_relay_pacing()['joined']` includes existing accepted bindings. It is cumulative, and an
edge's timestamp is the decision timestamp. A pre-deployment decision whose exit occurs after
deployment does not establish a newly attributable post-deployment decision.

Added `tests/test_relay_acceptance_cli.js`, following the existing Node/Python CLI test pattern.
Each fixture uses a private temporary Brain, handoff directory, and capability path, and invokes
the real CLI in separate bounded processes. Three tests verify:

1. A pre-deployment decision exiting in the observation window leaves the window counts at
   zero. Completing a fresh decision raises the cumulative count from one to two, while the
   same-window acted-on and outcome counts become one. Raw decision IDs, decision/exit
   timestamps, accepted edges, and persisted round IDs are asserted together.
2. An unreadable pacing log does not lose an explicit round or provider incident. Restoring
   the fixture input and retrying the identical fixture hook produces exactly one round,
   incident, and accepted join. This is an isolated recovery test, not a production replay.
3. An explicit exit prevents an older legacy decision from joining a later unlabeled exit.

Validation retained here:

- `node --test --test-isolation=none tests/test_relay_acceptance_cli.js`: **3 passed**, exit 0
  ([terminal output](relay-cli-tests.txt)). This runner disallows Node child-process pipes;
  file capture and a single-process test runner preserve execution of all assertions.
- `python3 src/rate_incidents.py --selftest`: **OK**, exit 0
  ([terminal output](rate-incidents-selftest.txt)).
- The combined new/pre-existing value-chain CLI test run: **11 passed, 4 failed**, exit 1
  ([terminal output](cli-tests.txt)). The four failures are existing preflight expectations
  that omit `consumer_acts`; the existing test and implementation match the assessed main
  byte-for-byte. They were not changed as part of this task.
- Focused pytest and `python3 -m pytest tests/ -q -m 'not slow'`: exit 1,
  **pytest is not installed** ([focused output](focused-pytest.txt),
  [repository output](repository-pytest.txt)). Installing test tools from PyPI failed DNS resolution.
- The exact requested `/opt/anaconda3/bin/python3 src/verify.py`: exit 127, interpreter absent
  ([terminal output](requested-verify.txt)). The available-interpreter run timed out at
  120 seconds with **exit 124 and no output** ([terminal output](available-verify.txt)).
  Neither run establishes verifier success.

No `.py` files were changed. JavaScript syntax and `git diff --check` pass.

## Production and deadline limits

[Environment readback](environment.json) at **2026-11-03T21:04:16Z** shows that the expected
relay pacing log, relay exit log, and Brain database are absent, with no path overrides set.
Production decision/exit/join counts are **unavailable**, not measured zero. The earlier
[host evidence](../issue-825-relay-host-integration.md) remains historical evidence only.
Generating an empty-runner switch report would not supply the requested production window,
so no such report is presented as acceptance. No speculative report-generation repair was made.

The original deadline is **2026-11-04T08:16:32Z**, still in the future at this observation.
Neither timely success nor a missed deadline can be established here. Any subsequent evidence
must use attributable decision and completion timestamps and distinguish later recovery from
timely acceptance.

`.verify-floor.json` is unchanged and byte-identical to assessed local `origin/main`
`e86313cb6f1f6f047896b5c1bbb5d9c3c68d339e`; both SHA-256 values are
`780186ee0caf07002daac988ecbd91c4d58efb8e1957adb164c8ebae25d0f849`.
[Comparison and source identity](source-and-floor.json) retain the result. GitHub access failed,
so this is the locally available remote-tracking revision, not a newly fetched main.

## Delivery blockers

Staging the new test failed with exit 128:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

The test and evidence remain in the working tree; no commit or push was possible. GitHub API
access also failed (`error connecting to api.github.com`), preventing remote PR readiness
inspection and publication of the blocker comment/`needs-human` label. Continue on a seat with
production read access, the requested Python/test tools, writable Git metadata, and GitHub access.
Only the review task is marked complete; all operational acceptance criteria remain open.
