# Issue 599: separate reviewed absence from M1 declaration failures

The remaining acceptance criterion requires `prose 0, missing 0`, with
`DRAINABLE` equal to the number of reviewed `consumer_missing` rows. Previously,
the report added those reviewed absences to `missing`, and the sync CLI rejected
them even after persistence. That made the criterion unattainable whenever a
reviewed absence remained.

`missing` now counts unresolved declarations. Reviewed absences retain their
separate count, explanation, DRAINABLE debt, failed admission, and lack of action
credit. They do not increase the number of findable consumers. The sync check
still rejects an empty live population, unpersisted reconciliation, prose,
unresolved symbols, and undeclared consumers.

Verification on 2026-11-12:

- `node --test --test-isolation=none tests/test_capability_consumer_sync_cli.js`
  passed all 11 tests, with no skips, in 118.08 seconds. The initial invocation
  using default isolation failed at file level without assertion diagnostics.
- Five focused Python test functions passed by direct invocation: declaration
  resolution, persisted declaration reconciliation, mixed absence/unresolved
  reporting, absence reasons, and clearing stale absence flags.
- The mixed absence/unresolved regression failed against the unchanged HEAD
  implementation in a disposable module copy: it printed `missing 2` where M1
  requires `missing 1` and a separate reviewed absence.
- Python compilation, Node syntax validation, and `git diff --check` passed.

The standard pytest command could not start because pytest is not installed.
Black formatting and the required repository-wide Black check could not start
because Black is not installed. Installing these tools in a temporary virtual
environment failed because this runner cannot resolve PyPI. No Python changes
may be committed until the required Black gate passes.

Staging the JavaScript regression failed because `.git/index.lock` cannot be
created on the read-only Git metadata mount. The implementation and tests remain
uncommitted. The actual live ledger is unavailable: the default CLI cannot open
`/home/runner/.codex/orchestrator` on this read-only runner. The owner must still
deploy/sync and capture actual live `CONSUMERS` and `CONSUMER SYNC` output using
`python3 src/capability_admission.py --check-consumer-sync`. Fixture results do
not complete the remaining acceptance checkbox.
