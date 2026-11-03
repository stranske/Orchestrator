# Native inventory WAL verification

Checked on 2026-11-03 against checkout `5bbc39549a51044692eb74910855a66799625e1e`.
The new `tests/test_adjudicator_native_inventory.js` exercises the real Python
inventory CLI and native journal producer through isolated temporary fixtures.
It verifies committed receipts still in SQLite WAL, separation of uncommitted
changes from a later committed observation, and refusal of dispatch/retry modes.
Original recursive paid identities, unresolved UNKNOWNs, foreign receipts and
untrusted canonical grade claims remain separately visible. Report, database
and WAL hashes remain unchanged by inventory; no Brain/state store is created.
The main-file-only control contains zero attempts while the live journal has
three, confirming that this test actually exercises WAL-resident evidence.

Validation:

- `node --test --test-isolation=none --test-reporter=tap tests/test_adjudicator_inventory.js tests/test_adjudicator_native_inventory.js`: 29 passed.
- `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_adjudicator_diagnostics.py -q`: 32 passed.
- The same unittest command with `test_adjudicator_replay_revision.py`: seven passed.
- `node --check tests/test_adjudicator_native_inventory.js` and `git diff --check`: passed.

These are fixture results, not production observations. The configured canonical
report, independent outcomes database and default execution mirror are absent
in this runner. No authentic original/journal inputs were supplied. No dispatch,
canonical refresh, October 18 proof or next-20 observation is claimed, and no
operational checkbox is complete. Pytest and Black are unavailable; installation
failed. This change edits no Python files and does not establish the required
full `tests/test_adjudicator_retro.py` result.

Local staging failed because `.git/index.lock` cannot be created on the read-only
Git mount. Committing the verified JavaScript file through GitHub's contents API
was also rejected because it requires approval and this run's policy is `never`.
No commit or push was made. PR #1081 was observed open with `draft=false`.

## Deployment and mirror CLI verification

Follow-up checked on 2026-11-03 against `afb27ee3affc11779d720558a6973f9b78f6befa`.
Two additional JavaScript integration tests exercise the real Python inventory
CLI with separate Git deployment and mirror fixtures. A flat mirror generation
inside the deployment checkout retains its selected/resolved paths and module
hashes without inheriting the parent revision. A changed deployment validator
updates its hashes and dirty status while the mirror and native receipts remain
unchanged. A dirty linked mirror worktree retains its own revision and hashes
despite inherited deployment Git locations. Neither observation grants a live
reader binding, historical proof, canonical grade authority or dispatch permission.

The inventory JavaScript command above now passes all 31 tests. The diagnostic
unittest command still passes all 32 tests; JavaScript syntax and `git diff --check`
pass. No Python files changed. The full retrospective pytest command with
`-m "not slow"` cannot start because pytest is absent in every installed Python
interpreter. The configured canonical report and independent outcomes database
are still absent. No authentic original/journal or runtime bindings were supplied.
No dispatch, historical observation, canonical refresh or fixed next-20 decision
is claimed, and reconciliation found no completed operational checkbox.

Staging the test change failed on the read-only `.git/index.lock` location; no
commit or push was made. Automatic approval review rejected PR-body reconciliation,
the `needs-human` label and the blocker comment because those tools require
approval and this run's policy is `never`. PR #1081 was rechecked open with
`draft=false` at the revision above.
