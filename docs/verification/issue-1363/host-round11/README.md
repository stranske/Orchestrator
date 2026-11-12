# Host round 11 ledger audit

T5 and A4 remain unmet. On November 11, 2026 in America/Chicago, the
existing hourly collector produced a fresh observation at
`2026-11-12T03:40:07.522329Z`. The audit ran at `03:40:09Z` from PR head
`0de107bfadc8dac6d09bf5a9b8306f54bd41e6da` and exited 1. The recount matched
the collector receipt across both retained ledger segments, with zero malformed
lines. The audit counted 147 branch-held and 41 branch-exists refusals and
printed `provision: failed(branch-held|branch-exists) = 188 over 7d`.

Coverage is UNKNOWN. The baseline starts at `2026-11-11T18:40:07.286665Z`,
about nine hours before the observation. The audit reported that seven
continuous observed days and intact continuous coverage had yet to be established.

The installed generation still tracks main revision
`e14cd229da94be574ec569618b529e10117bddd1`. Its provisioning and collector files
match the PR's Git blobs and the implementation hashes in the fresh receipt.
This audit used the PR reporter against that installed generation. The hourly
launch agent invokes the installed mirror with the default state and writer
paths. The shell and launchd exposed no overrides for `ORCH_STATE_DIR`,
`ORCH_LOCAL_RUNTIME`, or `ORCH_PROVISION_EVENTS_PATH`. Individual writer
process environments remain unverified.

```sh
runtime="$HOME/.codex/orchestrator"
generation="$(realpath "$HOME/.codex/orchestrator-mirror")"
ORCH_PROVISION_EVENTS_PATH="$runtime/provision-events.ndjson" \
  node scripts/audit_provision_ledger.js "$runtime" "$generation" \
  --observed-after 2026-11-12T03:32:02Z
```

The observation boundary records the start of this round's read-only watch.
The resolved generation was
`orchestrator-mirror.generation-f234323992db4266b8f79ad330f82c17` before and
after the audit. Both ledger segments and the collector receipt had identical
before and after SHA-256 hashes.

| File under the runtime directory | SHA-256 |
| --- | --- |
| `provision-events.ndjson` | `f07eb6f05f24c0c0c401611c762b8946b2bde69a0048868d43dc37f87ad24b88` |
| `provision-events.ndjson.pre-718-repair-2026-11-03` | `ccad083cf41cd3b6b839a8dd49fd6d1c651ce44458e364bf6e96ffb0a2ad1a76` |
| `followup-evidence/latest.json` | `c61cdd701b9271f3dacf534cb3b4da70858b1f7e88fee44a0e9e166c8f355a5a` |

For T5, rerun the audit after the regular deployment and the existing hourly
collector's first subsequent observation, using the deployment time as the
observation boundary. The hourly collector continues while acceptance is unmet
(blocking 1, drainable 1). A4 requires seven continuous observed days with zero
refusals. Every legitimate refusal remains in the count until it ages out.
