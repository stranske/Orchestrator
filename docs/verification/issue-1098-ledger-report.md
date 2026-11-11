# Issue #1098: read-only seven-day ledger report

Task reconciliation: reviewed bootstrap commit `4d55800d` and receipt-runner
commit `6f4b9262`. The latter changed three files, adding the native receipt
runner, its tests and validation notes. It completed no acceptance task. All
checkboxes remain open. The connector rejected the attempted PR-body
reconciliation update because it requires approval and this run uses approval
policy `never`.

The first two tasks remain blocked on deployment-host access and evidence. The
issue's November 10 receipt names outstanding capability-admission and heartbeat
audit failures; this runner has no deployment mirror, ledger, heartbeats or live
holder/scheduler state with which to reproduce or repair them. Local Python
lacks pytest and Black; installation into `/tmp` could not find an available
distribution. No verification floor or safety check has been weakened.

This iteration advances the third task with
`scripts/audit_provision_ledger.js`. Run it on the deployment host with the
runtime directory and the directory containing the observed Python sources:

```sh
node scripts/audit_provision_ledger.js \
  /absolute/path/to/orchestrator-runtime \
  /absolute/path/to/installed-mirror-generation
```

The report prints the existing collector's `continuous_since` baseline,
observation time, raw failure counts and required
`provision: failed(branch-held|branch-exists) = N over 7d` line, then a JSON
receipt containing source and segment hashes. It reads
`followup-evidence/latest.json` and every retained `provision-events.ndjson*`
segment, including archives when the active filename is absent. It does not
write state, rotate ledgers, run a collector, publish evidence or acquire a
production writer lock.

Counts preserve duplicate events and legitimate dirty/live-holder refusals.
The existing collector's owner exclusion applies before parsing and hashing,
as it does in `followup_evidence.eligible_lines`; this report introduces no new
exemptions. Recounted counts, segment hashes and implementation hashes must
match the collector observation. A changed, appended, renamed or missing segment
requires a fresh observation by the existing collector. No new scheduler is
introduced. Corrupt or partial rows, missing sources, stale/future observations,
continuity failures and a baseline younger than seven days produce UNKNOWN.
Without a usable receipt, visible events are still counted through the current
time; no continuity is inferred from those events.

Exit zero means only that the existing collector reports an intact observed
week and the checked ledger has zero branch refusals. Native tests, full
verification and mirror acceptance still need their own receipts;
`deployment_acceptance_verified` is always false. Nonzero counts are explicitly
reported as unmet acceptance, without changing their original outcomes.

Validation on this hosted runner:

- `node tests/test_audit_provision_ledger.js`: 29 passed, zero skipped. Fixtures
  execute the real Python collector through 169 hourly observations in temporary
  directories. Cases cover archives, multiplicity, microsecond boundaries,
  corruption, segment changes, source drift, missing/stale receipts, CLI exit
  status and read-only behavior. These are fixture observations, not deployment
  receipts or a real elapsed week.
- Existing pacing-ledger and native-witness runner regressions: 8 and 14 passed,
  respectively, zero skipped.
- Node syntax checks and `git diff --check` passed.
- `python3 src/verify.py` exited 1 on the working tree: pytest collected zero
  tests against floor 8595 because pytest is missing; 97 of 111 selftest modules
  ran, five skipped and nine failed. Admission failed on missing pytest;
  entrypoint checks reported the read-only `/home/runner/.codex/orchestrator`
  path. Runtime writes also appear in the selftest diagnostics. Complete command
  output is retained locally at `/tmp/issue1098-verify-after.log`. These hosted
  runner failures do not reproduce the deployment's two outstanding audits.
- `.verify-floor.json` bytes match `origin/main`; SHA-256:
  `12cea5203329d538fc12312c9f48b7f08686b2efd2c82c41115dd46365bcd1f6`.

No task is marked complete: deployment evidence, the delivered-commit full
verifier run and the original acceptance criteria remain required.

Commit attempt: `git add` could not create `.git/index.lock` because `.git` is
mounted read-only. The three files remain uncommitted; no alternate branch,
remote commit or permission bypass was attempted.
