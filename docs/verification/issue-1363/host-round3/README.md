# Host keepalive round 3 — 2026-11-11

Outcome: **0 of T1, T2, T4 and T5 verified complete**. Source tested:
`9a208ad26e3119b89162aaf6d0baa409bdcf7452`. This delivery adds observations only;
no implementation, dependency pins, floor, production state or deployment changed.
The selected interpreter is `/opt/anaconda3/bin/python3` (Python 3.12, pytest 9.2.0).

## T1 — historical custody remains unknown

`refusal-observation.json` retains the provision section from the existing hourly
collector, checked at `2026-11-11T23:40:07.702839+00:00`. Its newest refusal is
Workflows #4637 at `2026-11-11T16:59:47.763115+00:00`, naming holder #4635 and
PID 49989. `target-state.json` retains current target-specific readback: the
holder and holder claim are absent, and neither current dispatcher inventory has
a matching record. The local-loop record retains three failed starts and a
handed-off status. That is insufficient to reconstruct the effective reservation
and writer lock at refusal time. The prior round's event readback records a later
successful reclaim, but success does not establish historical ownership.

The existing stale-orphan fix is already in main through #1344 (`21e48b27`).
No new avoidable cause was demonstrated here, so no speculative reclaim change
was made. Historical custody and a new demonstrated cause remain unverified.

## T2 — native restoration fails closed

The existing test already checks both lanes, the holder inode, HEAD and branch
after disabled reclaim, then restores the real function and retries native
probes. The exact focused command is:

```sh
/opt/anaconda3/bin/python3 -m pytest -q tests/test_provision_reclaim.py -k native_reclaim_disabled_exit_128_names_holder
```

`same-holder-native.log` retains both failures and `same-holder-native.exit`
records exit 1. The disabled attempts reach the expected git exit 128 and holder
survival assertions; both restored attempts fail because `pgrep` exits 3:
`sysmond service not found; Cannot get process list`. This is not a verified
successful native reclaim. No process-probe or privacy/security behavior changed.

`protections.log` retains nine passing focused tests covering dirty holders,
live holders, failed/blind probes, live-holder drain reporting and the existing
stale-orphan regression. These passes do not establish writer-lock custody or
replace the failing native restoration witnesses.

## T4 — verifier exceeds the bounded execution window

`verify-context.json` records the command, source SHA, start time, runtime
overrides and 120-second timeout. `/opt/anaconda3/bin/python3 src/verify.py`
produced no output before timeout. `verify.log` retains the complete captured
output plus the harness termination notice; `verify.exit` is 124. The harness
sent SIGKILL to its verifier process group and reaped the parent. Descendant
termination was not independently enumerated. No named failure, including a
capability-admission or heartbeat failure, was printed, so no implementation
repair can be attributed to this run. The timeout is not a passing verifier.

`floor-comparison.json` records equal SHA-256 hashes and byte equality of
`HEAD:.verify-floor.json` and the fetched `origin/main:.verify-floor.json`.
The floor was not changed. This comparison alone does not satisfy A3.

## T5 — deployment and seven-day acceptance remain unmet

`ledger-provenance.json` records the exact read-only auditor command with the
effective runtime and resolved installed generation, and before/after hashes
of both retained segments. Both stayed byte-identical. `ledger.log` and
`ledger.exit` retain exit 1, 188 refusals, UNKNOWN coverage, and the reported
segment mismatch with the collector observation. No legitimate refusal was
discounted. No deployment occurred, and the collector observation was not fresh
postdeployment evidence for this branch. A4 and T5 remain unmet.

The checked acceptance boxes in the task prompt do not establish deployment
acceptance on this host. The existing hourly collector remains the observation
owner; no collector, writer, reservation or holder was replaced or released.
Host continuation requires native process visibility, historical custody
evidence, a successful delivered verifier, and a fresh collector observation
after deployment. This sandbox cannot write the installed generation.

Post-push review: the next scheduled keepalive owns readback after the seven-minute
floor. No merge is attempted. The existing minor documentation review comment
was observed; it is outside this acceptance push and remains follow-up work.
