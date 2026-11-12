# Host keepalive round 2 — 2026-11-11

Delivered source checked: `28291fa7` on `orch/issue-1363`. This round changes
receipts only. No production deployment, collector invocation, lock release,
ledger edits, dependency changes, or process-probe weakening was performed.

The selected interpreter is `/opt/anaconda3/bin/python3` (Python 3.12, pytest
9.2.0). `contract.md` records the outcome command and bounded stop rule.

- T1 remains incomplete as specified. The newest collector refusal is Workflows
  #4637, holding the former #4635 opener directory with live PID 49989.
  `latest-refusal.json`, `target-events.json`, and `target-state.json` retain
  target-specific readback. The holder and current claim are absent and the
  current dispatch inventory has no matching record; historical reservation and
  writer-lock ownership remain UNKNOWN. The existing orphan fix is already in
  main via #1344. Earlier deliberate-break evidence remains at
  `/Users/teacher/.codex/orchestrator/measurement/issue-1363/stale-orphan-break.log`;
  that receipt alone does not establish the missing historical ownership.
- T2's source implements the same-inode/head/branch survival assertions and native
  restore/retry for both lanes. This host cannot verify the native restoration:
  `native-provision.log` records 52 passes and six failures. Native `ps` is denied
  (`Operation not permitted`), and `pgrep` reports `sysmond service not found`.
  Fail-closed behavior is retained. T2 is not claimed complete in this round.
- T3 is verified by `native-runner-tests.log`: 39 pass, no skips, using the
  explicit selected interpreter for both real Python paths. Tests cover installed
  runtime exclusions, symlink/import/config rejection and private input copies.
  The actual installed-generation command rejects the branch because its runner
  bytes differ from the incumbent generation (`installed-native.log`). This is
  expected provenance protection and does not establish A2 deployment acceptance.
  The first invocation lacked its output parent; the retained retry supplied it.
- T4 remains incomplete. `verify-context.json` records the exact command, source
  revision, runtime overrides and 120-second limit. The complete captured output
  is `verify.log`; the command produced no aggregate report before timeout (124).
  No named failure was printed, so there is no demonstrated implementation failure
  to repair. Timeout killed the verifier parent; descendant termination could not
  be established under denied process enumeration. No passing full verification
  or native process lifecycle is claimed. The floor byte comparison passes.
- T5 and A4 remain unmet. The existing hourly collector's observation is dated
  2026-11-11T23:40:07Z; this is not a fresh postdeployment observation of this
  branch. The read-only audit exits 1 with 188 refusals (147 branch-held and 41
  branch-exists), UNKNOWN coverage, and segments differing from the collector's
  observation. `ledger-provenance.json` confirms both retained segments stayed
  byte-identical across our audit. Legitimate refusals are not discounted.

The supplementary protection-suite attempt and its exit status are retained in
`protections.log` and `protections.exit`. No failing safety test was weakened.
No acceptance criterion is claimed complete. T3 alone is verified this round.
Post-push review belongs to the next scheduled keepalive after the seven-minute
review floor; no merge was attempted.
