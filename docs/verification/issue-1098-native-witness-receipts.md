# Issue #1098: native witness receipt runner

This change advances receipt binding for the native provisioning task. No task or
acceptance checkbox is complete. The default provision suite, same-holder
disable/restore cycle, deployment full verifier, mirror provenance acceptance,
and intact zero-refusal seven-day window still require their original evidence.

`scripts/run_provision_native_witnesses.js` runs the existing unmodified
`test_native_clean_idle_holder_is_reclaimed` and
`test_native_reclaim_disabled_exit_128_names_holder` cases for both lanes on an
explicitly selected flat mirror. It requires four successful named JUnit cases;
exit zero without those cases, skips, missing pytest, or malformed XML cannot
produce a successful witness receipt. The disabled-reclaim witness itself checks
Git exit 128 and the holder path. This runner does not add a same-holder restoration
witness or claim that the entire provision suite passed.

The runner compares installed flat Python modules, the test tree, configuration,
and runner/helper bytes with the selected committed Git objects. It records a
SHA-256 manifest of the mirror's regular files before execution and checks it
again afterward. Ordinary pytest/bytecode caches are excluded; symlinks,
uncommitted test inputs, and extra root Python modules are refused. Receipts live
outside the mirror in a new private directory. The tested floor must match
`origin/main` byte for byte. The resolved mirror generation, revision, host,
interpreter, commands, log/XML hashes, and named verdicts are retained.
`deployment_acceptance_verified` always remains false: passing these four cases
does not prove deployment identity, full verification or seven-day acceptance.

After committing and installing these files, run on the deployment host, choosing
its actual interpreter and immutable installed mirror generation:

```sh
node scripts/run_provision_native_witnesses.js \
  --python /absolute/path/to/python3 \
  --revision <full-delivered-commit-sha> \
  --mirror /absolute/path/to/installed-mirror-generation \
  --output /absolute/path/to/new-receipt-directory
```

Run from the checkout at that revision with the required `origin/main` reference.
The timeout is per subprocess, defaults to 60 seconds, and can be shortened with
`--timeout` (milliseconds). Test state and provisioning events use temporary paths;
the runner does not dispatch targets, sync a mirror, or edit production ledgers.

Local validation on the hosted runner:

- `node tests/test_provision_native_witnesses.js`: 14 passed, zero skipped. Pytest
  execution is doubled in these regression tests; XML parsing uses real stdlib
  Python. These are runner tests, not native deployment evidence.
- `node tests/test_adjudicator_acceptance_runner.js`: 33 passed, zero skipped
  (existing shared subprocess-helper regression suite).
- Node syntax checks and `git diff --check`: passed.
- `.verify-floor.json` bytes equal the checked-out `origin/main` version;
  SHA-256 `12cea5203329d538fc12312c9f48b7f08686b2efd2c82c41115dd46365bcd1f6`.

The deployment ledger and mirror are absent here. The default Python has no
pytest or Black, and package installation failed because PyPI DNS/network access
is restricted. `python3 src/verify.py` exited 1: zero pytest cases collected
(floor 8595), admission failed for missing pytest, and selftest/entrypoint
diagnostics include the read-only `/home/runner/.codex/orchestrator` path. The run
reported 97 of 111 selftest modules ran. Its local complete output is retained at
`/tmp/issue-1098-verify-before.log`; these runner failures do not reproduce the
deployment's two remaining audit findings. No full-suite PASS is claimed.
No Python files or verification-floor settings were changed.
Committing the JavaScript changes was attempted, but `.git` is
mounted read-only and Git could not create `index.lock`; the changes remain
uncommitted in this workspace.
