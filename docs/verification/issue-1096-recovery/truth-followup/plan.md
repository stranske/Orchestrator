# Retained later-truth diagnostics, 2026-11-03 UTC

Recent commits through `9c00d5bda5a429e48310a2bd5e7b27c4f5b30fba` were reviewed.
They do not establish required pytest execution or operational acceptance.
Recovery remains partial: accepted/deployed/mirror bindings, original retry
manifest and fixed next-20 manifest are unavailable. Only the pre-execution
inventory and ledger tasks, plus attribution/disclosure criterion, are verified.
The attempted PR-body reconciliation was rejected because the connector requires
approval while this run's policy is `never`. No remote update is claimed.

Run the required checks once, before source edits:

```sh
node scripts/run_adjudicator_acceptance.js \
  --python /opt/hostedtoolcache/Python/3.14.9/x64/bin/python3 \
  --revision 9c00d5bda5a429e48310a2bd5e7b27c4f5b30fba \
  --output /home/runner/work/Orchestrator/Orchestrator/docs/verification/issue-1096-recovery/truth-followup/acceptance-run
```

Expected: pytest is unavailable before collection; preserve both required test
commands and their failures without claiming success. Ordinary and isolated
installation attempts found no distribution, and the local interpreter/cache
search found no pytest copy. Stop after this invocation, with the runner's
60-second per-command bounds and `-m "not slow"`. The selected revision is the
checkout, not a verified accepted deployment.

With that task blocked, add read-only diagnostics for retained later truth.
Distinguish missing, explicit UNKNOWN, invalid and binary-but-unverified values;
bind each recorded value by location and hash, without deriving truth from the
adjudicator decision or borrowing truth across parent/retry attempts. Preserve
paid history, saved decisions and all existing receipt/authentication boundaries.
Verify new regressions fail before implementation and pass afterward. Exercise
the CLI on the already-committed historical diagnostic projection, which is not
the original authenticated report or requested retry cohort.

No dispatch, production write, canonical publication or mirror synchronization.
No grading or operational task is complete from diagnostic classifications.
