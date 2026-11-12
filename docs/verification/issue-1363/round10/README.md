# Delivered-checkout prerequisite reproduction

Both remaining tasks stay unchecked. This Linux runner has neither the installed
mirror generation nor the existing hourly collector's runtime. No deployment,
fresh host observation, successful full verifier, or seven-day acceptance is
claimed. The checked acceptance boxes in the task prompt are not supported by
the retained results; the incumbent host-round7 audit still records 188
legitimate refusals and unmet acceptance.

The full verifier attempt on `db3072ee8e6f5a5ab36d4df6d27e55293951b1d0`
finished at `2026-11-12T02:18:13.765Z` with exit 1. Its complete output is
`verify.log` and `verify.stderr.log`; `verify-context.json` and `verify.exit`
retain the receipt and process verdict. It collected zero pytest cases against
floor 8677, passed 106 module selftests, skipped five modules for named missing
prerequisites, and failed `local_verify` and admission because pytest is missing.
The activation audit passed. The receipt records the test edit made during
execution, so this attempt is not a final clean-delivery receipt even apart from
the verifier failure. HEAD stayed unchanged.

The source change extends `tests/test_provision_verify.js` with a real
`verify.run_pytest` regression. It excludes installed packages only at child
launch (`-S`), leaving the production subprocess, parser, and receipt runner in
use. It asserts the missing-pytest diagnostic, nonzero exit, zero collected and
passed tests, absent JUnit inventory and verdicts, and failed acceptance despite
matching floor bytes. All 11 receipt-runner tests pass in `runner-tests.log`.
`collection-mutation.log` shows this new regression failing when a disposable
copy of the production parser invents one collected test after the same launch
failure. Repository production code was not changed for that experiment.

The selected interpreter is
`/opt/hostedtoolcache/Python/3.14.10/x64/bin/python3`. Both available package-index
attempts to install pytest and Black into `/tmp/orch1363-round10-python` fail;
their output is retained in the dependency-install logs. No Python source or
protected files changed. Direct `local_verify.py --selftest` and standalone
admission-gate reproductions both identify missing pytest. The admission
reproduction uses `PYTHONPATH=src:.`, matching the checkout module layout.
These failures do not establish a capability-admission or heartbeat logic defect.

`floor-comparison.json` binds both retained committed floor blobs to the tested
revision and local `origin/main`, records their SHA-256 hashes and sizes, and
records `cmp` exiting zero. The original floor bytes remain in the external
receipt directory `/tmp/orch1363-round10-verifier`. No floor was edited.

`deployment-paths.json` records the absent host runtime, mirror alias, and host
interpreter. Auditing a fabricated local ledger would not satisfy the deployment
task. Existing retained host segments and collector receipts were not changed.

The required source/test commit is blocked by the filesystem permission profile:
Git cannot create `.git/index.lock` on the read-only `.git` directory. No branch
or pull request was created.

The GitHub API was unreachable for the blocker comment, `needs-human` label,
and PR readiness read; their errors and exit codes are retained separately.
`blocker-comment.md` contains the unpublished handoff. No remote task boxes
were changed, and PR readiness could not be verified.
