# Collector continuity regression follow-up

The two remaining tasks stay unchecked. This runner cannot complete full Python
verification or perform the postdeployment collector audit.

`verify-context.json` identifies the checked-out revision
`9a208ad26e3119b89162aaf6d0baa409bdcf7452`, selected Python 3.14.10 interpreter,
exact command, and temporary runtime overrides. `verify.log` retains the complete
output of `python3 src/verify.py`; `verify.exit` records exit 1. It collected zero
pytest cases against a floor of 8636. Of 111 module selftests, 105 passed, five
reported prerequisite skips, and `local_verify` failed. The admission gate failed
with missing pytest; the activation audit passed.

`prerequisites.log` confirms that none of the installed Python interpreters has
pytest or Black and reproduces missing pytest in the provision suite and admission
gate. `local-verify-reproduction.log` reproduces the first named module failure:
pytest is not importable with the selected interpreter. The isolated dependency
installation also failed; its output and exit status are retained. No admission or
heartbeat implementation defect was established here. No Python source was edited.

`floor-comparison.log` retains the requested byte comparison with the local
`origin/main` ref, identifying both revisions; it exits zero. The floor is unchanged.

While the first unchecked task is blocked, two new integration cases in
`tests/test_audit_provision_ledger.js` advance the collector audit protections. They
exercise the real collector over disposable active and archived segments containing
both lanes' refusals and successful outcomes. A fresh observation at exactly two
hours preserves the continuous baseline; two hours plus one microsecond resets it.
After the old refusals age out, resumed hourly collection still cannot qualify a
zero-refusal week until a new seven-day baseline exists. All retained segment bytes
survive. These observations are test fixtures, not deployment receipts.

`ledger-tests.log` records all 45 auditor tests passing with no skips and the
explicit selected Python. The pre-change suite also passed. `gap-mutations.log`
records the new cases detecting two disposable mutations: allowing a longer
collector gap and accepting zero refusals without continuous coverage. The
repository collector and auditor implementations were unchanged. Node syntax and
`git diff --check` pass; see `checks.log`.

`deployment-paths.log` records that the known deployment interpreter, runtime and
installed mirror generation are absent here. A deployment host with the delivered
checkout and matching installed generation must still run full verification and
native suites, then audit the effective writer and collector paths after a fresh
observation by the existing hourly collector, retaining every archive and refusal.

`staging.log` records `git add` failing because `.git` is mounted read-only and Git
cannot create `index.lock`. No commit was created; the source change and receipts
remain in the working tree. The connector rejected both the blocker comment and
`needs-human` label because approval is required but approval policy is never;
see `connector-blockers.log`. Readback in `pr-readiness.json` confirms PR #1365 is
open with `isDraft=false` at the revision above.
