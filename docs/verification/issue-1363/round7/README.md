# Installed-source baseline regression

The two remaining tasks stay unchecked. This runner has neither the deployment
runtime nor its installed generation, and its selected Python cannot import
pytest. These local tests are not deployment receipts.

`tests/test_audit_provision_ledger.js` now runs the real collector against changed
copies of the installed provision and collector sources. Each case starts with
an observed week and two archived refusals, verifies that the old receipt rejects
the changed source, then takes fresh observations with that source. The refusals
age out of the count, but the source change resets the observation baseline.
Another hourly observation has matching source hashes and still cannot qualify
the zero-refusal window. All three ledger segments remain byte-identical.

`ledger-tests.log` records 49 passing tests without skips, using the absolute
selected Python. `baseline-mutation.log` records both new cases failing when a
disposable collector copy omits its source-change baseline checks. No production
source or runtime was changed by that mutation. Syntax and whitespace checks
pass in `checks.log`. No repository Python file changed.

`verify-context.json` identifies the tested checkout revision, interpreter,
private runtime overrides and start time. `verify.log` and `verify.exit` retain
the complete verifier output and exit status. `local-verify-reproduction.log`
independently reproduces the missing-pytest prerequisite. The dependency
installation attempt failed; its output and status are retained.

The full command exited 1: pytest collected zero tests against floor 8636,
105 module selftests passed, five reported prerequisite skips, and `local_verify`
failed because pytest is not importable. The admission gate also failed with
missing pytest, independently reproduced in `admission-reproduction.log`; the
activation audit passed. No capability-admission or heartbeat
implementation defect was demonstrated. A passing verifier on the delivered host
is still required.

`floor-comparison.json` records the exact Git-object comparison against the local
`origin/main`, names both revisions and hashes, and records exit zero. The floor
was not modified. `deployment-paths.log` records the absent deployment paths.
There was no deployment or fresh observation from the deployment host's existing
hourly collector, so no live ledger acceptance is claimed.

Task reconciliation reviewed commits through `85c88f8c`, including the hosted
same-holder evidence in `831bca84` and the failed verifier/deployment evidence in
the previous rounds. The three implementation tasks remain checked. The native
deployment, passing delivered verifier and deployment-ledger acceptance boxes
must be unchecked: prior receipts do not establish those claims. The PR-body
update, blocker comment and `needs-human` label were each rejected by automatic
approval review because approval policy is `never`.

`staging.log` records `git add` failing because `.git` is read-only. Consequently
the requested source/test commit could not be created; the changes remain on
the checked-out PR branch. `pr-readiness.json` records PR #1365 open and ready
for review. No branch, pull request, protected file or workflow was created or
modified.
