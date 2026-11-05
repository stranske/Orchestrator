# Evidence ledger audit plan

Recorded on 2026-11-03 UTC before execution. The source checkout under test is
`d84ae822cb6181d53e9a993712807dfc49fd41a5`; its accepted deployment remains
unverified. The available CI-major interpreter is
`/opt/hostedtoolcache/Python/3.14.9/x64/bin/python`.

```sh
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python \
  --revision d84ae822cb6181d53e9a993712807dfc49fd41a5 \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/ledger-audit-tests
```

Expected: collection and both required test invocations fail before collection
because pytest is unavailable. Preserve their revision-bound logs. One invocation
per command, with the runner's 60-second limit and `-m "not slow"`; do not repeat
installation attempts. Local package/cache discovery found no usable pytest.

Then run the new ledger audit once against `evidence-ledger.json`. Expected:
separate evidence categories and referenced local receipt hashes are checked;
unavailable accepted/deployed/mirror bindings, authenticated native receipts,
original retry/cohort provenance and October 18 raw evidence remain disclosed.
Historical statements and local log integrity cannot establish operational
acceptance. No provider calls, journal writes, publication or mirror writes.

Recent commits were reconciled before proceeding. Recovery is partial, the
pre-execution inventory is implemented, and no retrospective test, retry, grade
or fixed-cohort task is complete. The remote PR-body correction was rejected
because connector mutations require approval while approval policy is `never`.
The ledger publication task will be completed locally only after its audit and
regressions pass; remote updates must not be claimed without a successful write.
