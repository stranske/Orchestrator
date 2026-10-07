The live two-profile × three-instance trial remains unchecked. This runner has no
configured `ORCH_STATE_DIR`, no frozen strategy packet, and no configured OpenAI
authentication. The operator's Mac experiment artifacts are unavailable here.
No live trial result was generated.

The CLI regression tests now verify that rejected evidence leaves no rows or
receipt, and that correcting the evidence allows the same frozen trial to record
all six instances. They check quality, usage cost, and provider-resolved identity
per profile, and confirm a replay preserves the receipt. Additional rejection
cases cover invalid quality, malformed usage, and an identity artifact with a
matching digest but a model that contradicts the attempt. Fixtures are synthetic;
these checks do not satisfy the live-trial task.

Validation:

- `node --test --test-isolation=none tests/test_profile_trial_cli.js`: 9 passed.
- `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_profile_trial_instances.py -v`: 12 passed.
- Both trial module selftests passed; JavaScript syntax and `git diff --check` passed.
- The named pytest command could not run because pytest is unavailable.
  Installation of Black and pytest failed. This change edits no Python files.

Attempts to add `needs-human`, post the blocker comment, and inspect PR #498's
open/non-draft state failed because `api.github.com` is unreachable. No remote
mutation succeeded.

The original checkout's `.git` directory is read-only, so staging and committing
there failed with `Unable to create .git/index.lock: Read-only file system`.
The changes remain in this checkout; an isolated checkout under
`/tmp/profile-trial-498-delivery` holds the delivery commit and patch.

Operator follow-up: use the existing frozen recurring-work packet and an
authorized transport with provider-resolved identity evidence, collect six real
attempts, and finalize into the intended
`$ORCH_STATE_DIR/capability-program/profile-trial.json`.
