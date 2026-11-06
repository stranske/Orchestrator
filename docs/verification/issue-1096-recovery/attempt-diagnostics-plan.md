# Retained attempt diagnostics, 2026-11-03 UTC

Recent commits through `6749ae79c00be3c9362a01ce117e46dc304d931e` were reviewed.
Recovery remains partial; only the pre-execution inventory and evidence ledger
tasks and the attribution/disclosure criterion are verified complete. Duplicate
PR checklists need the same states. The connector rejected the body correction
because approval is required and the run policy is `never`.

The selected interpreter still reports `ModuleNotFoundError: No module named
pytest`. Do not repeat package installation. Before source edits, preserve one
bounded required-test attempt against this checkout:

```sh
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 \
  --revision 6749ae79c00be3c9362a01ce117e46dc304d931e \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/attempt-diagnostics/acceptance-run
```

Expected: collection and both groups fail before collection; required tests stay
incomplete. Stop after one invocation per command, each at most 60 seconds, using
`-m "not slow"` and temporary state. This checkout is not an accepted deployment.

With the test task blocked, advance retry reconciliation by preserving ordered
attempt observations, including nested paid attempts, with locations and hashes
of failure fields. Explain retained failures without copying private error text
or inferring an unrecorded provider/validator failure stage. Distinguish a saved
binary decision, abstention, missing decision and contradictory saved evidence.
Attach observations only to uniquely matched manifest identities. Saved truth,
native markers and complete identity matching cannot authenticate outcomes.

Verify Node regressions, CLI parity, retained input immutability, failure hash
binding and protection against treating nested failures as a failed parent
decision. Do not dispatch, publish, alter production state, write mirrors, select
a replacement cohort or claim operational acceptance. No protected or Python
files are required. Git metadata is read-only; retain source commits and a patch
using an isolated Git directory under `/tmp`.
