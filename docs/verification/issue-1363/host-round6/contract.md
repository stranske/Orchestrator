Check date 2026-11-11 America/Chicago. The operational outcome is a continuous
seven-day observation with zero branch-held or branch-exists refusals, printed by
`node scripts/audit_provision_ledger.js "$runtime_dir" "$installed_generation"`
after the existing hourly collector observes the delivered deployment. For the
observed avoidable refusal, use the existing stale-orphan regression to reproduce
the failure and verify recovery. The separate verifier requires bare
`/opt/anaconda3/bin/python3 src/verify.py` and byte-identical floors.
Stop rule: one deliberate break and one restoration, one exact-head floor
comparison plus a repeat after fetch if main advances, one audit with a repeat
only if its inputs change during the read. Stop local work after 20 minutes.
Blocking 1, drainable 1: incomplete deployment evidence leaves current ownership
and retained ledger segments in place while the existing collector continues.
