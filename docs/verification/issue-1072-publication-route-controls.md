# Native publication route controls

Checked on 2026-11-08 against checkout
`2dd564469dc47ac5084681f5e4e22259fb3b33bb` plus the accompanying JavaScript tests.

The first incomplete operational task remains blocked: the configured canonical
report, independent outcomes database, runtime directory and default mirror are
absent. Authentic immutable-original and journal locations, and live reader
bindings, have not been supplied. A checkout revision is not a deployed revision.
No task checkbox has been changed.

Three new CLI controls advance the next task, reconciliation of the requested
`--dispatch --retry --limit 21` procedure with native publication protection:

- The actual generic entry point refuses the selected native canonical report
  and directs the caller to `--native-admit` followed by `--native-publish`.
  A guarded execution also forbids outcome reads, evidence collection, role
  dispatch, history repair, journal initialization and report persistence.
- Native publication rejects a stale original-source hash before projection,
  independent-truth reads, reservation or dispatch.
- Native publication rejects a stale canonical hash at the same boundary.

Original, canonical, main journal and committed WAL bytes remain unchanged.
The generic refusal retains recursive original paid identities and UNKNOWNs in
a subsequent inventory, without authenticating retained grading claims.
Native publication controls use an empty manifest to reach the pin checks;
they do not exercise a successful cohort refresh or establish eligibility.

The retained-report, native inventory and live-cohort JavaScript suites pass all
193 tests, including the three new controls. Removing each corresponding guard
in a separate temporary source copy makes its new test fail. Existing diagnostic and
replay/publication unittests pass all 39 checks. The required
`python3 -m pytest tests/test_adjudicator_retro.py -m 'not slow'` cannot start
because pytest is absent. Installing Black and pytest into a temporary directory
failed because no Black distribution was available. No Python files changed.

## Additional native-path blocker

The initial stale-pin test using the inventory fixture's `native #1?.sqlite`
journal failed before publication: `NativeAttemptJournal.open_existing` reports
`native cohort requires a native journal`. An isolated reproduction also creates
an unexpected file named `native ` beside that valid journal. The opener builds
a SQLite URI by interpolating the filesystem path, so URI metacharacters change
the selected location and can prevent the read-only query option from applying.
The inventory reader already uses `Path.as_uri()` and observes the intended file.

The stale-pin controls select an ordinary filename so they can independently
verify publication admission. This does not fix or excuse the opener defect.
Repair the URI construction and add a metacharacter-path regression in an
environment that can run the required Black formatting gate before committing
Python changes.

These results are local fixture observations. No paid retry, canonical refresh,
mirror synchronization, October 18 proof, next-20 decision, or authenticated
grade is claimed. Production and historical acceptance remain incomplete.

Staging is blocked by the read-only `.git/index.lock` location. The focused test
change is preserved using isolated Git metadata and
`/tmp/orchestrator-1072-publication-controls/change.bundle`; this does not update
the checkout branch or remote PR.
