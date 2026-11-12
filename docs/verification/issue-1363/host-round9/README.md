# Host round 9 ledger audit

T5 and A4 remain unmet. The incumbent generation was deployed at
2026-11-12T02:31:30Z and the existing hourly collector observed it at
2026-11-12T02:40:04.998670Z. The audit ran at 2026-11-12T03:02:09Z
(November 11, 2026 in America/Chicago) and exited 1. It recounted 188
refusals in the observed seven-day window, including 147 branch-held and 41
branch-exists. Coverage is UNKNOWN because the baseline is shorter than seven
days and the live ledger bytes differ from the observation. The active and
archived segments and collector receipt have identical before and after hashes.

The mirror deployment history and symlink identify the installed revision as
`e14cd229da94be574ec569618b529e10117bddd1`. The auditor ran from PR revision
`2b33cd53466e616936e7db5d117ed8d808e69b4e`. The installed generation lacks
the PR delivery. The hourly launch agent calls the mirror at minute 40 and
supplies no state or writer overrides. The shell and launchd have no overrides
for `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME` or `ORCH_PROVISION_EVENTS_PATH`.
The receipt and live ledger are at the installed defaults. Environments of
individual writers were not independently inspected.

```sh
runtime=/Users/teacher/.codex/orchestrator
generation=/Users/teacher/.codex/orchestrator-mirror.generation-f234323992db4266b8f79ad330f82c17
ORCH_PROVISION_EVENTS_PATH="$runtime/provision-events.ndjson" \
  node scripts/audit_provision_ledger.js "$runtime" "$generation" \
  --observed-after 2026-11-12T02:31:30Z
```

The existing hourly collector continues observing while acceptance is blocked.
Rerun after the PR delivery is deployed and that collector has observed it.
Acceptance requires a continuous seven-day baseline and zero refusals. Every
legitimate refusal remains in the count until it ages out of the window.
