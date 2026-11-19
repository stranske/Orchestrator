# Host round 10 ledger audit

T5 and A4 remain unmet. At 2026-11-12T03:16:29Z (November 11 in
America/Chicago), the PR reporter at `bc48af51085695a5a8a9219238d0d404c901631b`
audited the incumbent generation. It exited 1 and printed
`provision: failed(branch-held|branch-exists) = 188 over 7d` (147 branch-held,
41 branch-exists). The count ends at the existing hourly observation at
2026-11-12T02:40:04.998670Z. The baseline starts at
2026-11-11T18:40:07.286665Z. Coverage is UNKNOWN because seven continuous
days have yet to be established and the active ledger has grown since the
collector observation. Both retained segments and the collector receipt had
identical SHA-256 hashes before and after the audit.

The active generation remains the one documented in [round 9](../host-round9/README.md)
at main revision `e14cd229da94be574ec569618b529e10117bddd1`. The installed
`provision.py` and `followup_evidence.py` each match the PR and main Git
blobs byte for byte. The audit ran from the PR checkout. The hourly launch
agent uses the installed mirror at minute 40 and supplies no writer override.
The shell and launchd reported no state, runtime, or writer-path overrides.
Individual writer environments were outside this readback.

```sh
runtime="$HOME/.codex/orchestrator"
generation="$(realpath "$HOME/.codex/orchestrator-mirror")"
ORCH_PROVISION_EVENTS_PATH="$runtime/provision-events.ndjson" \
  node scripts/audit_provision_ledger.js "$runtime" "$generation" \
  --observed-after 2026-11-12T02:31:30Z
```

The boundary above is the incumbent deployment time. For T5, use the
delivered generation's deployment time and the next observation from the
existing hourly collector. The normal mirror deployment tracks `origin/main`.
The collector continues while acceptance is unmet (blocking 1, drainable 1).
A4 requires seven continuous observed days with zero branch refusals.
