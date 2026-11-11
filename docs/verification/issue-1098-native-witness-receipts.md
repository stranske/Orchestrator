# Issue #1098: native witness receipt runner

This change advances receipt binding for the native provisioning task. No task or
acceptance checkbox is complete. The default provision suite, same-holder
disable/restore cycle, deployment full verifier, mirror provenance acceptance,
and intact zero-refusal seven-day window still require their original evidence.

`scripts/run_provision_native_witnesses.js` runs the
`test_native_clean_idle_holder_is_reclaimed` and
`test_native_reclaim_disabled_exit_128_names_holder` cases for both lanes on an
explicitly selected flat mirror. It requires four successful named JUnit cases;
exit zero without those cases, skips, missing pytest, or malformed XML cannot
produce a successful witness receipt. The disabled-reclaim witness checks Git
exit 128 and the holder's path, branch, commit, and directory identity. It then
restores reclaim, retries using native process probes, and checks the replacement
checkout and the ledger event identifying the original holder. The named-witness
log retains the Git refusal and the successful reclaim output.

The runner compares installed flat Python modules, the test tree, configuration,
and runner/helper bytes with the selected committed Git objects. It runs pytest
from a private copy of the authenticated inputs read from the pinned generation.
It records the mirror manifest and its SHA-256 digest before execution and
recalculates the digest afterward. Symlinked code, tests and pytest configuration,
uncommitted test inputs, and unexpected Python modules are refused. Receipts live
outside the mirror in a new private directory. The tested floor must match
`origin/main` byte for byte. The resolved mirror generation, revision, host,
interpreter, commands, log/XML hashes, and named verdicts are retained.
`deployment_acceptance_verified` always remains false: passing these four cases
does not prove deployment identity, full verification or seven-day acceptance.

The private tree omits the following runtime paths from the installed layout.
They cannot supply Python imports or pytest configuration to the run.

- Root `.venv`, `.pytest_cache` and `.ruff_cache` and regular `.pyc` files inside
  `__pycache__` directories. Pytest caching is disabled and bytecode writes are
  disabled. Select an interpreter outside the installed generation. For the
  mirror's retained virtual environment, use its external backing directory's
  `bin/python3` path so Python can still locate `pyvenv.cfg`.
- Generated `docs/reports`, `ux_reviews`, and experiment run output
  (`experiments/tick-<timestamp>-<target>-<number>`, `.last-ship-gate`, `invman1`,
  `orchskill-lanes-20260620`, and `strategy434-20261006` under `experiments/`).
- Legacy retained root files `path.txt`, `private-ledger.lock`,
  `repo_review_registry.json.bak-2026-09-21`, `README.md`, `renovate.json`, and
  `uv.lock`. The `campaigns` link is omitted only when it points to the matching
  storage-maintenance data path in a retained sibling generation. The runner
  never follows the link.

After committing and installing these files, run on the deployment host, choosing
its actual interpreter and immutable installed mirror generation:

```sh
node scripts/run_provision_native_witnesses.js \
  --python /absolute/path/to/python3 \
  --revision <full-delivered-commit-sha> \
  --mirror /absolute/path/to/installed-mirror-generation \
  --output /absolute/path/to/new-receipt-directory \
  --full-suites
```

`--full-suites` also collects and executes all selected cases in
`tests/test_provision_reclaim.py` twice: once with
`ORCH_PROVISION_NATIVE_ACCEPTANCE` absent and once with it set to `1`. Both modes
use `-m "not slow"`. Separate logs, commands, collection node IDs, JUnit hashes
and verdicts are retained under `suites` in the receipt. Each suite must execute
exactly its collected cases without skips, errors or failures. Empty or duplicate
collections, missing or extra verdicts, nonzero commands and source drift cannot
produce `default_native_suites_passed=true`. Passing the four named witnesses
alone cannot satisfy this flag. With this option, the CLI exits nonzero if either
suite fails, even when the named witnesses pass. Without the option, the original
four-witness behavior is retained and the full-suite flag stays false.

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

## Full-suite runner follow-up (2026-11-11)

Reconciliation reviewed `4d55800d`, `6f4b9262` and `27b29395`: none completed
an original task or acceptance criterion, so all checkboxes remain unchecked.
The PR-body reconciliation update was rejected by the connector because it
requires approval and this run has approval policy `never`.

The optional suite execution advances the native evidence task while deployment
access blocks the first task. The issue's latest host receipt still names
capability-admission and heartbeat-audit failures; this runner has no installed
mirror, live scheduler/holder state or production ledgers. Installing pytest and
Black into `/tmp` failed with no available distribution. No Python, workflow,
verification-floor or process-safety code was changed. A same-holder native
disable/restore cycle and deployment identity still require evidence;
`deployment_acceptance_verified` remains false even if both suite flags pass.

Local validation:

- `node tests/test_provision_native_witnesses.js`: 28 passed, zero skipped.
  Pytest output is simulated; these validate the runner, not deployment behavior.
  Cases include default/native environment separation, full-suite omissions,
  failures, errors, skips, extra or misattributed verdicts, failed/empty/duplicate
  collections, source drift and CLI exit status.
- `node tests/test_adjudicator_acceptance_runner.js`: 33 passed, zero skipped.
- Default and native `python3 -m pytest tests/test_provision_reclaim.py -q
  -m "not slow"` attempts both exited 1 because pytest is missing. The working-tree
  `python3 src/verify.py` attempt was interrupted (exit 130) after five minutes
  without a report; its redirected output remained empty. No completed full
  verification receipt or passing provisioning-suite result is claimed.
- `.verify-floor.json` is byte-equal to `origin/main`; SHA-256 remains
  `12cea5203329d538fc12312c9f48b7f08686b2efd2c82c41115dd46365bcd1f6`.
- Node syntax checks and `git diff --check` passed. Staging the three changed
  files failed: `.git` is mounted read-only and Git cannot create `index.lock`.
  No commit was created. The blocker comment and `needs-human` label were also
  rejected by the connector's approval requirement. PR #1351 was verified open
  with `isDraft=false`.

## JUnit reader diagnostics follow-up (2026-11-11)

The first task remains blocked on deployment logs/runtime access. The latest
owner receipt names the local-loop declaration/recent-heartbeat mismatch and
ambient `f1`/`test_feature` heartbeat audit findings; this checkout has no deployed
mirror or production state with which to reproduce them. No original task or
acceptance checkbox is complete.

The native witness runner now retains each JUnit reader's command, exit status,
signal, error code, JSON decoding result and private output log with its SHA-256
hash. This applies to the named witnesses and both full suites. Malformed XML,
nonzero readers and invalid JSON still fail acceptance; missing XML does not
invent a reader run. Previously these diagnostics were discarded and a failed
reader left only `cases: null` in the receipt.

The existing Python environment isolation regression now poisons stdlib `json`
instead of requiring a real pytest package while the fixture simulates pytest.
Its baseline failed here solely because pytest is absent. Removing `-E` in a
temporary copy makes the updated regression fail, confirming it still detects
inherited module substitution.

Validation of the working tree:

- Native receipt runner regressions: 33 passed, zero skipped, using
  `PYTHON=$(command -v python3) node tests/test_provision_native_witnesses.js`.
  These are fixture tests, not native deployment receipts.
- Shared subprocess-helper regressions: 33 passed, zero skipped.
- Actual default and native provision-suite attempts both exited 1 because
  `python3 -m pytest` reports `No module named pytest`.
- Node syntax checks and `git diff --check` passed.
- The floor is byte-equal to `origin/main`, SHA-256
  `12cea5203329d538fc12312c9f48b7f08686b2efd2c82c41115dd46365bcd1f6`.
- `python3 -u src/verify.py` was interrupted with exit 130 after it produced no
  aggregate report. Full verification is unverified. Python has no pytest or
  Black; installation into `/tmp` failed because PyPI DNS is unavailable. No
  Python files or verification-floor settings were edited.
- `git add` failed because `.git` is mounted read-only and Git cannot create
  `index.lock`; no commit was created. The connector rejected both the
  `needs-human` label and blocker comment with `MCP tool call requires approval,
  but approval policy is never`. PR #1351 remains open with `draft=false`.
