# Effective writer-path verification follow-up

Both remaining tasks stay unchecked. The deployment host runtime, selected host
interpreter and installed generation are absent from this runner; the disposable
collector observations below are regression fixtures, not deployment receipts.

`tests/test_audit_provision_ledger.js` now checks both directions of the explicit
writer-path override. Each case imports the actual `provision` module with the
same environment as the auditor CLI and verifies its chosen event path. A fresh,
continuous zero-refusal receipt cannot qualify when the explicit writer is outside
the observed directory. Conversely, an observed explicit writer takes precedence
over a different runtime directory. The receipt, active ledger, retained archive
and separate ledger containing both lanes' refusals remain byte-identical.

`ledger-tests.log` records all 47 auditor tests passing, with no skips, using the
selected absolute Python interpreter. `writer-override-mutation.log` records both
new tests failing when a disposable copy of the auditor ignores the explicit
writer override; its other 45 tests pass. Repository implementation files were
unchanged by that mutation. `checks.log` retains passing syntax and whitespace
checks. No Python file was edited.

`verify-context.json` identifies the checkout revision, selected interpreter and
private runtime overrides for the full `python3 src/verify.py` command. Complete
output and exit status are retained in `verify.log` and `verify.exit`.
The command exits 1: pytest collected zero cases against floor 8636; 105 of
111 module selftests passed, five reported prerequisite skips, and `local_verify`
failed. The admission gate failed with missing pytest; the activation audit passed.
`local-verify-reproduction.log` reproduces the named local verifier failure:
pytest is not importable. `admission-reproduction.log` independently confirms
missing pytest in the admission gate. Both isolated dependency installation
attempts failed; their output is retained. No demonstrated admission or heartbeat
implementation defect was repaired, and no process-probing protection changed.

`floor-comparison.log` and its exit file retain the exact requested Git-object
byte comparison with the local `origin/main`, identify both revisions, and confirm
byte equality with exit 0. The floor was not modified. `deployment-paths.log`
records the absent deployment paths. Host verification and a fresh postdeployment
observation by the existing hourly collector are still required, preserving all
archives and counting every legitimate refusal as unmet acceptance.

`staging.log` records `git add` failing with exit 128 because `.git` is mounted
read-only. No commit could be created; the test changes remain in the working
tree on the checked-out PR branch. `connector-blockers.log` records remote
publication refusals under approval policy `never`.
`pr-readiness.json` confirms PR #1365 is open with `isDraft=false` at the tested
revision. No workflow, protected file or production runtime was modified.
