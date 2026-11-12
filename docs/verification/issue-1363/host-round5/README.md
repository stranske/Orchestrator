# Host keepalive round 5 — 2026-11-11 America/Chicago

Outcome: **0 of T1, T4, T5 verified complete**. Source checked:
`20de36e54271300f17b018c0c33103e47f9002a3`. This commit adds evidence only.
The remote branch advanced before work began; the local branch was fast-forwarded
before checking. No implementation, protected pins, production state, or
collector scheduling changed.

Acceptance outcome and commands (check date 2026-11-11 local / November 12 UTC):
reproduce and repair an observed avoidable refusal, obtain exit zero from
`/opt/anaconda3/bin/python3 src/verify.py`, and obtain continuous seven-day zero
refusals from `node scripts/audit_provision_ledger.js /Users/teacher/.codex/orchestrator <resolved-installed-generation>`.
Stop rule: two attempts per failing command or 45 minutes, with acceptance
commands bounded to 120 seconds. Blocking/drainable pair: refusal-time custody
UNKNOWN and deployment acceptance unmet / existing holders, locks and retained
segments preserved while the existing hourly collector continues observation.

## T1: custody still unknown

`collector-provision.json` retains the existing hourly observation checked at
`2026-11-12T00:40:04.907857+00:00`. `latest-refusal.json` selects its newest row:
Workflows #4637 at `2026-11-11T16:59:47.763115+00:00`, naming opener holder #4635
and PID 49989. `target-state.json` retains targeted dispatcher and local-loop
readback. The holder and both current claims are absent; no exact target record
was found in either current dispatcher inventory. The local-loop state records
three failed starts and a handoff. None establishes the reservation or writer
lock at refusal time. Do not infer that a historical live-PID refusal was safe
to bypass from today's missing holder.

The already-present stale-orphan regression explicitly names this holder and
historical failure. The focused command below passed six cases (exit 0), including
the orphan repair, CPU-active process preservation, dirty holders, live probe
results and failed process probing. No new avoidable refusal or missing fix was
demonstrated, so no speculative implementation change was made.

```
/opt/anaconda3/bin/python3 -m pytest -q tests/test_provision_reclaim.py -k 'stale_orphan or using_cpu or dirty_holder or live_probe_result or process_probe_failure'
```

## T4: verifier timed out, floor bytes match

`verify-context.json` records the selected interpreter, exact source SHA, command,
private writable runtime overrides and start date. `verify.log` retains all
captured output: the verifier emitted no output before its 120-second timeout;
`verify.exit` records harness exit 124. This is UNKNOWN verification, not a pass
or a reproduced capability-admission/heartbeat failure. No named failure was
printed, so there is no evidence-bound code repair to make. The harness terminated
the verifier on timeout; descendant termination could not be established because
process enumeration (`ps`) is denied in this sandbox.

`floor-comparison.json` retains the byte equality and SHA-256 for the checked
revision and fetched `origin/main`: both are
`2b0df1cb5b074ffb3cebb3187fc8b092095f1ebf193917d91b9fa82e0a881580`.
A3 still requires the successful verifier, so it remains unmet.

## T5: real ledger audit remains unmet

`ledger-audit.log` retains exit 1 and **188 refusals**: 147 branch-held and
41 branch-exists. It reports incomplete seven-day coverage and active/archive
segments differing from the collector observation. `ledger-provenance.json`
records the resolved installed generation and identical before/after hashes of
both retained segments. Every legitimate refusal remains counted.

No deployment occurred: the installed generation and effective runtime are
outside this worktree's writable scope. This observation predates this delivery
and cannot satisfy the required fresh postdeployment collection. T5 and A4 remain
unmet. The existing hourly collector remains the observation owner; no competing
collector was started and no lock or holder was released.

Follow-up evidence needed: refusal-time reservation/writer custody, successful
verification on the delivered source, and postdeployment collector evidence
with a genuinely continuous seven-day zero-refusal window. The existing local
keepalive owns the subsequent review pass after the push's seven-minute floor.
