# Required retrospective test run, 2026-11-03 UTC

Recorded before executing the new runner. Evaluate source revision
`42ee516d3219c98129a9f4837a1edd35937148d8`, whose parent is checkout revision
`0ecb2c1102b7043b74f905774d91b485d7d7fe27`. The two added JavaScript files provide
test execution and evidence validation; the retrospective Python source is
unchanged. Neither revision is verified as the accepted production deployment.

Normal staging failed with `.git/index.lock: Read-only file system`. The source
commit is in the isolated Git directory below, using this repository as its work
tree. The original checkout branch has not changed.

```sh
GIT_DIR=/tmp/issue-1096-acceptance-git-1gfr70z1 \
GIT_WORK_TREE=/home/runner/work/Orchestrator/Orchestrator \
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python \
  --revision 42ee516d3219c98129a9f4837a1edd35937148d8 \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/acceptance-run
```

Expected current outcome: both pytest invocations fail before collection because
the selected interpreter has no pytest installation. The runner must preserve
both commands and logs with unsuccessful status, null test counts, and
`required_tests_passed=false`. Its exit status must be 1. The existing package
cache and installed Python locations did not supply pytest; the prior network
installation failure is not retried. No replacement test framework is used.

Bounded stop rule: execute the four named tests once and the full
`tests/test_adjudicator_retro.py` suite once, both with `-m "not slow"`, each
with a 60-second timeout. Use a temporary test state directory, disable third-party
pytest plugin autoload, and clear extra pytest options. Do not call the production
adjudicator CLI, dispatch, publish, initialize a production journal, or write a
mirror. An absent, empty, malformed, skipped, duplicate, or unrelated JUnit result
cannot establish a passing run. Refuse pre-existing output directories and
uncommitted source/test inputs; invalidate results if inputs change during testing.

The revision and tree identify repository inputs only. A passing future run still
requires independent accepted/deployed revision evidence to satisfy the complete
acceptance criterion. This run cannot complete the test task while pytest and
that deployment binding are unavailable.
