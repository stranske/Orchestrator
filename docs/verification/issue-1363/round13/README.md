# Require a collector observation after deployment

The remaining post-deployment audit task stays unchecked. The known production
runtime and mirror paths under `/Users/teacher/.codex/` are absent on this Linux
runner. The attempted audit exits 1 with ENOENT (`deployment-audit.log`). No fresh
host observation or deployment acceptance is established. The retained
`host-round7/ledger-audit.log` reports 188 refusals; it is historical evidence.

The auditor now accepts `--observed-after TIMESTAMP`, also exposed as the
`observedAfter` API option. This addresses an ordering gap: an otherwise intact,
recent receipt could precede a deployment of the same source bytes. With this
option, `checked_at` must be strictly later than the caller's timezone-qualified
boundary. Equal timestamps, including equivalent timezone offsets, fail at
microsecond precision. Invalid boundaries fail closed without discarding counts.
Both text and JSON reports retain the boundary. The original two-argument CLI
remains supported, and deployment identity still needs separate evidence.

On the host, record the actual deployment completion time and installed generation
from deployment evidence, wait for the existing hourly tick, then run:

```sh
node scripts/audit_provision_ledger.js "$runtime_dir" "$installed_generation" \
  --observed-after "$deployment_completed_at"
```

Use the writer's effective `ORCH_PROVISION_EVENTS_PATH` or `ORCH_LOCAL_RUNTIME`
configuration when running the audit. The option does not run the collector,
reset the baseline, change the seven-day window, or edit retained segments.
Every legitimate refusal remains unmet acceptance.

Validation:

- All 55 existing auditor tests passed before changes.
- Running the new tests against the original committed auditor in a disposable
  `/tmp` layout produced three failures and 55 passes (`ledger-before.log`).
  The API incorrectly accepted the receipt at the boundary, and the old CLI did
  not support the new option.
- `PYTHON=$(command -v python3) node tests/test_audit_provision_ledger.js` passes
  all 58 tests, zero skipped (`ledger-tests.log`). The three new cases cover
  strict timestamp ordering and invalid input, plus real collector continuation
  with empty ledgers and duplicate refusals in both lanes. Receipt and segment
  bytes remain unchanged by the auditor.
- Both JavaScript syntax checks and `git diff --check` pass. No Python, workflow,
  verification floor, production event, holder, or writer lock was changed.

PR #1365 was read back as open with `draft=false`. No task checkbox was changed.

The requested source/test commit could not be created. Staging failed with:

```text
fatal: Unable to create '/home/runner/work/Orchestrator/Orchestrator/.git/index.lock': Read-only file system
```

The prepared files remain in the checked-out branch's working tree. Automatic
approval review rejected both the blocker comment and `needs-human` label with
`MCP tool call requires approval, but approval policy is never`. Neither remote
mutation was applied. Host access and a writable Git checkout are still required.
