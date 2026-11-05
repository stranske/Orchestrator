# Interpreter portability follow-up, 2026-11-03 UTC

Before execution: evaluate source revision
`96dde1810f56562e0e23af6f0e603b3e48e7728a`, parent checkout
`1ecef8633d696c6e3b1ad864572dad65bea132dc`. This identifies repository inputs;
the accepted production deployment remains unverified.

The current environment selects Python 3.14.10. Seven runner regressions failed
before this change because their JUnit-parser fixture hardcoded a nonexistent
3.14.9 installation. The fixture now resolves its real parser interpreter from
`PYTHON`, or `python3` on PATH. Pytest commands remain test doubles in that fixture.
The production runner still requires an explicitly selected absolute interpreter.
It now saves JUnit-reader output and status separately from pytest output/status.

Run the existing inventory and updated runner regressions once, then invoke:

```sh
GIT_DIR=/tmp/issue1096-interpreter-git-oe_clm4j \
GIT_WORK_TREE=/home/runner/work/Orchestrator/Orchestrator \
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.10/x64/bin/python3 \
  --revision 96dde1810f56562e0e23af6f0e603b3e48e7728a \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/interpreter-followup/acceptance-run
```

Expected result: 26 runner regressions and 40 inventory regressions pass. The real
collection, four named tests, and full retrospective suite fail before collection
with missing pytest, and the acceptance runner exits 1 with
`required_tests_passed=false`. Interpreter patch portability and parser diagnostics
do not complete the required-test task.

Stop after one collection and one invocation of each required test group, each
bounded to 60 seconds and using `-m "not slow"`. Use the runner's temporary state
and disabled plugin autoload. Do not attempt another package installation, dispatch,
publish, initialize production state, or write a mirror. Do not alter the accepted
deployment claim or any task checkbox based on test-double results.

Normal staging failed with `.git/index.lock: Read-only file system`. The source
commit is in the isolated Git directory above, referencing the original checkout's
object store. A format-patch will preserve it for application to checkout HEAD.
