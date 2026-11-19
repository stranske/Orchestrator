# Host round 12 ledger audit

T5 and A4 remain unmet. On November 11, 2026 in America/Chicago
(`2026-11-12T03:47:20Z`), the read-only audit exited 1. It counted 188
refusals through the existing hourly collector's `03:40:07.522329Z`
observation, with 147 branch-held and 41 branch-exists across the active and
archived segments. There were zero malformed lines.

The host still uses main revision `e14cd229da94be574ec569618b529e10117bddd1`.
The provisioning and collector source hashes match the receipt and the
corresponding files at PR head `8c9e087359e6c0497eab15d0c118cf789a1164bc`.
The hourly launch agent runs the mirror at minute 40. The launcher and writer
defaults resolve to the paths below. The shell and launchd exposed no state
or writer overrides. Individual writer environments remain unverified.

```sh
runtime_dir="$HOME/.codex/orchestrator"
installed_generation="$(realpath "$HOME/.codex/orchestrator-mirror")"
ORCH_PROVISION_EVENTS_PATH="$runtime_dir/provision-events.ndjson" \
  node scripts/audit_provision_ledger.js "$runtime_dir" "$installed_generation" \
  --observed-after 2026-11-12T02:31:30Z
```

The boundary is the incumbent deployment time. The resolved generation was
`orchestrator-mirror.generation-f234323992db4266b8f79ad330f82c17` before and
after the audit. The following hashes matched before and after the run.

| File under the runtime directory | SHA-256 |
| --- | --- |
| `provision-events.ndjson` | `1e205912196d35f6292c83f4d5b3f3ad23c6d693f366a6119ca3d254a00f2bd9` |
| `provision-events.ndjson.pre-718-repair-2026-11-03` | `ccad083cf41cd3b6b839a8dd49fd6d1c651ce44458e364bf6e96ffb0a2ad1a76` |
| `followup-evidence/latest.json` | `ebc9bb4165dbbbf6aa7b06c664432440bc506a5995b7d7b1ca9137f79c39acd5` |

Coverage is UNKNOWN. The baseline starts at `2026-11-11T18:40:07.286665Z`,
only nine hours before the observation. The active segment grew by one
non-refusal after the collector tick, so the auditor also reported a segment
mismatch. Both observed prefixes still hash to the receipt. The newest
refusal in the observed window was branch-held at
`2026-11-11T16:59:47.763115Z`.

After the regular PR deployment, use its publication timestamp as
`--observed-after` and audit after the existing hourly collector observes
that generation. Acceptance requires a continuous seven-day baseline and
zero refusals with all segments retained. The collector continues while
acceptance is unmet (blocking 1, drainable 1).
