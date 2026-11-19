# Host keepalive round 7

Outcome: 188 branch-held/branch-exists refusals; zero of T1, T4 and T5
verified complete. Checked November 11, 2026 America/Chicago. No production
code, dependency pin, ownership record or retained ledger was changed.

## T1: existing repair verified, historical custody unknown

`live-correlation.json` starts from the newest refusal in the existing hourly
collector's November 12 01:40 UTC observation: Workflows #4637, refused by the
#4635 opener holder with PID 49989. The holder and both current target claims
are absent. The current closer dispatch inventory has neither exact target.
These current absences do not establish the effective reservation or writer-lock
owner at the refusal time; those remain UNKNOWN.

The existing ledger records reclaim of that same holder at
2026-11-11T17:14:49.572600+00:00. The demonstrated stale-orphan cause already
has a repair in #1344 (`21e48b27`), present in both delivery and installed code.
`stale-orphan-before.log` reproduces the live-PID refusal by disabling only
`_stale_orphans` through an external temporary pytest plugin. Restoring the
real function passes the regression and focused dirty-holder, native-claim,
live-holder and fail-closed probe checks. The process metadata in that
regression is controlled; this is not a new native host witness.
After merging main, the same six focused cases passed again; their complete
output is in `reclaim-protections-final.log`. All 39 native-runner Node tests
also passed with `PYTHON=/opt/anaconda3/bin/python3`, recorded in
`native-runner-tests.log`. Those Node tests do not establish A2's separate
installed-generation full-suite witness.

No new repair is justified by this observation. T1 remains unchecked because
the requested historical custody correlation is incomplete.

## T4: floor synchronized, bare verifier pending

The initial floor comparison failed after main advanced to `c9656865`.
The branch merged that main revision without manually editing the floor;
the final comparison is retained separately. No dependency pins changed.
Git completed the merge but its subsequent maintenance reported a denied
`packed-refs.lock`; HEAD and branch status confirmed the merge commit exists.

The bare `python3 src/verify.py` command invokes the full local pytest suite.
This keepalive explicitly prohibits running the full suite locally. It was
therefore not run, and no substitute is claimed as T4 or A3 acceptance.
The selected interpreter is `/opt/anaconda3/bin/python3`; the checkout has
no explicitly selected virtual environment. CI on the pushed head remains
responsible for full-suite execution. No new capability-admission or heartbeat
failure was observed or repaired.

## T5: incumbent audit remains unmet

`ledger-audit.log` retains the complete read-only audit against the installed
generation resolved in `ledger-provenance.json`, using the default effective
writer and collector directory `/Users/teacher/.codex/orchestrator`.
The launch agent invokes `orchestrate.sh --active` at minute 40; its collector
uses `ORCH_STATE_DIR` or that default, and provisioning uses
`ORCH_PROVISION_EVENTS_PATH`, `ORCH_LOCAL_RUNTIME`, or that default. Neither
writer override is set in this seat or the launch-agent plist; inherited
environment overrides in other writers were not independently established.

The audit exits 1 with 147 branch-held and 41 branch-exists refusals, incomplete
seven-day coverage, and active segments differing from the collector snapshot.
Both retained segments and the collector receipt remain byte-identical before
and after this audit. The collector observation is fresh, but this PR head
has not been deployed. No collector was manually invoked, no observation was
fabricated, and no baseline or event was removed. T5 and A4 remain unmet.

The existing hourly collector continues while acceptance is blocked. A passing
audit requires the delivered deployment and seven continuous observed days with
zero legitimate refusals; an audit of the incumbent cannot establish that.
