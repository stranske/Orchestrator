# Issue 1363 verification follow-up

The delivery and live ledger tasks remain **unchecked**. This is a GitHub runner,
with no installed mirror generation or hourly collector runtime at the standard
host paths. No deployment, collector observation, or seven-day acceptance is claimed.

The checkout tested is recorded in `initial-revision.txt`. The selected interpreter
is `/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3` (Python 3.14.10).
`initial-verify.log` retains the output of `python3 src/verify.py`, run with
`PYTEST_ADDOPTS='-m "not slow"'`; it exits 1 after collecting zero pytest cases,
with nine failed module selftests and a failed admission gate. Its exit status is
recorded separately. The interpreter has no pytest. A separate prerequisite probe
and direct admission-gate invocation reproduce that missing-module error.
Dependency installation into `/tmp/orch-1363-venv` also fails; see
`dependency-install.log`. No dependency or interpreter files were added to the checkout.
Black is also unavailable; this round changes JavaScript tests only.

The first named selftest failure, `capability_firing_monitor`, reproduces with
`OSError: [Errno 30] Read-only file system` at the default runtime directory.
It passes unchanged with `ORCH_STATE_DIR` pointing to a temporary directory under
`/tmp`; both complete outputs are retained. The activation audit itself reports
`ok` in the full verifier. No capability-admission or heartbeat implementation
defect has been established by this runner, so neither implementation was changed.

`floor-comparison.log` records the exact byte comparison of the checked-out
revision's `.verify-floor.json` with `origin/main` and identifies both revisions.
The comparison exits zero. The floor was not edited or reconciled.

Since the first unchecked task is blocked on test prerequisites, this round advances
the next task's reporting protection with a CLI regression in
`tests/test_audit_provision_ledger.js`. It uses the real collector on disposable
inputs: both lanes have dirty-holder, live-holder, writer-lock, and branch-exists
refusals in retained archives, followed by successful retry events in the active
segment. A fresh, consistent collector receipt still yields eight refusals,
continuous coverage, unmet acceptance, and CLI exit 1. All receipt and segment
bytes survive the audit unchanged. These disposable observations are test inputs,
not deployment receipts.

Validation: all 39 ledger-auditor tests and all 39 native-witness runner tests pass;
their complete outputs and exit statuses are retained here. `node --check` and
`git diff --check` pass. The native-runner tests include simulated Python commands;
they do not establish that the actual native suites ran on an installed generation.
`ledger-cli-mutation.log` records the new regression failing when a disposable
copy of the auditor is changed to return CLI success unconditionally. The
repository's auditor was unchanged during this mutation check.

The GitHub API is unreachable from this environment (`gh pr view 1365` fails to
connect), so a remote blocker comment, `needs-human` label, and PR readiness check
could not be published or verified. Host follow-up still requires a provisioned
interpreter, the delivered checkout and installed generation, full native receipts,
and a fresh observation by the existing hourly collector with all archives retained.

Commit blocker: `git add tests/test_audit_provision_ledger.js
docs/verification/issue-1363` failed with `Unable to create
'.git/index.lock': Read-only file system`. The sandbox grants read access to
`.git` only, so no commit was created. The source change and evidence remain in
the checked-out branch's working directory for staging and committing outside
this sandbox.
