# Retained dispatch refusals, 2026-11-03 UTC

First unchecked task: run the four named tests and full retrospective suite on
checkout `441f22ebb3550d88c42061d85fa1275cd1da8116` with the selected interpreter
`/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3`:

```sh
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 \
  --revision 441f22ebb3550d88c42061d85fa1275cd1da8116 \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/dispatch-refusal-followup/acceptance-run
```

Expected: missing pytest prevents collection and both test invocations. Preserve
each command and its unsuccessful output. Stop after this runner invocation;
its collection and two checks each have a 60-second limit and skip slow tests.
The checkout is not established as the accepted deployment.

If blocked, advance retry diagnostics by classifying the two exact retained
launcher refusal messages in the recovered historical backup
`dropbox:id:tDSHdCztYNUAAAAAADfgrw`. Keep their origin distinct from messages
emitted by repository code. A refusal is not a provider timeout, completed
decision, authenticated outcome, or authorization to retry. Unknown or modified
messages must remain unresolved. Preserve original field locations and hashes.

Validate existing Node tests before edits, add focused refusal controls, and run
the inventory on a projection of the recovered report retaining only identity,
decision, failure and retry-history fields. Bind the projection to the extracted
report hash; do not describe it as the complete original report or native cohort.
Do not dispatch, publish, write production state, or sync a mirror. Original
retry/cohort manifests, authenticated receipts and live writer ownership remain
unavailable; leave acceptance checkboxes open.
