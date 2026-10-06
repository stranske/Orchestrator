# PR #498: six-instance CLI verification and live-trial blocker

The live two-profile × three-instance trial remains unchecked. On this runner,
`ORCH_STATE_DIR` is unset and the strategy experiment's frozen recurring-work
packet is absent. The existing strategy receipt locates the live artifacts on
the operator's Mac. The subscription bridge explicitly leaves provider-resolved
identity unavailable, so its CLI identity evidence cannot complete this task.

`tests/test_profile_trial_cli.js` now exercises `prepare` and `finalize --ingest`
in separate processes with a synthetic frozen packet and six synthetic attempts.
It verifies the exact capability-program output path, per-profile quality,
both usage directions, provider identity, shared capacity debit, database row
counts, and idempotent replay. Five rejection cases verify that unverified
identity, unavailable provider identity, a changed identity artifact, changed
source bytes, and a missing instance produce neither trial rows nor result
artifacts. All fixture evidence and runtime output stay in temporary directories;
none is a live-trial result.

Validation:

- `node --test --test-isolation=none tests/test_profile_trial_cli.js`: 6 passed.
- `PYTHONPATH=src python3 -m unittest discover -s tests -p test_profile_trial_instances.py -v`:
  12 passed.
- `python3 src/model_profile_trial.py --selftest`: passed.
- `python3 src/model_profile_trial_bridge.py selftest`: passed.
- `git diff --check`: passed.
- The requested targeted pytest command could not execute: pytest is not installed.
  Installing pytest and Black failed because `pypi.org` could not resolve.
  This change edits no Python files; the Python pre-commit formatting gate does
  not apply to the JavaScript test.

GitHub access also fails with `error connecting to api.github.com`. The attempt
to read PR #498's state failed, so its open/non-draft state could not be verified.
Attempts to add `needs-human` and post this blocker as a PR comment also failed
with the same connection error; this local receipt preserves that report.

The required commit could not be made: staging failed with
`fatal: Unable to create '.git/index.lock': Read-only file system`.
The JavaScript test and this receipt remain uncommitted in the working tree.

Required follow-up: make the existing frozen packet available in an environment
with an authorized transport that records provider-resolved identity, collect
six real attempts with quality and usage, and finalize their verified evidence
into `$ORCH_STATE_DIR/capability-program/profile-trial.json`. Run the named pytest
checks and verify the PR's CI Gate and open/non-draft state there. Do not use the
synthetic CLI test receipt to mark the live trial complete.
