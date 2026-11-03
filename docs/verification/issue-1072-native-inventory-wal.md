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
