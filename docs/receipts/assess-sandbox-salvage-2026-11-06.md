# PR #1141 reduced scope — 2026-11-06

Outcome 1: retained adapter and interval-reporting contracts pass focused verification;
this is branch evidence, not automatic fallback or operational acceptance.

Before edits, the acceptance command and stop rule were recorded in the task chat:
`/opt/anaconda3/bin/python3 -m pytest tests/test_dispatcher_offload.py` must pass
within two minutes, dispatcher must match main, and the retained module must occur
exactly once in `config/test-shards.json`. Stop after three focused repair attempts
or 45 minutes. Check date: 2026-11-06. A real-input reporting command was run first:

```text
/opt/anaconda3/bin/python3 src/switch_review.py --assess-sandbox-fallback --since 2026-11-06T00:00:00Z --ledger /Users/teacher/.codex/orchestrator/capacity-ledger.ndjson --json
```

It printed `status=unmeasured`, `reason=capacity ledger absent`, and null counts.
That initially supplied path was absent. Reading the actual default ledger with
`--ledger` omitted printed `status=unverified`, `fallback_runs=0`,
`network_caused_assess_incompletions=1`, and `window_complete=false`.
The start is an inspection date, **not** an implementation merge timestamp.
These results establish reporting behavior only; neither is acceptance evidence
for the removed runtime path or the required seven-day operational window.

## Retained

- `src/adapters.py::build_command` explicit sandbox selection and refusal to widen
  restricted profiles or explicit read-only permissions.
- `src/switch_review.py` interval helpers, CLI and reporting integration.
- Adapter/reporting tests only in `tests/test_dispatcher_offload.py`, assigned once.
- Baseline and historical RED/GREEN receipts, labeled branch evidence only.

The bootstrap marker change and all dispatcher automatic probe/fallback changes
are removed. No dependency pins are changed by this salvage. Main was merged
before reduction; existing main changes are not salvage work.

## Preserved runtime follow-up — issue #1133 remains open

Original branch revision: `a459028c330375ec1d9567f0090a8775f6a5bb83`.
It remains an ancestor of this branch and has a local safety ref
`salvage/1141-runtime-a459028`. Recover the dispatcher and integration tests from
that revision; their preservation is not a claim that they work.

- Establish a supported installed-Codex probe invocation using the assess child's
  effective network/filesystem policy and environment. Remove or justify each
  probe-only override with retained real execution evidence.
- Restore and finish `_sandbox_has_network` and offload integration, preserving
  permission refusal, exact no-write preamble, diagnostic, sandbox selection and
  start/complete `sandbox_fallback` ledger flags.
- Restore integration assertions; use a controlled monotonic clock or command
  construction delay to prove reduced remaining allowance and no subprocess
  launch after exhaustion. Add command-policy regression coverage.
- Assign the restored module exactly once; retain selected-interpreter sandbox,
  `src/verify.py`, and refreshed deliberate-break/revert outputs under receipts.
  This salvage runs focused tests locally; full verification belongs to CI.
- After runtime implementation merges, execute the dispatcher assess command in
  `assess-sandbox-network-baseline-2026-11-05.md`. Retain date, raw `gh api rate_limit`
  JSON, sandbox selection, terminal result, run_id and matching ledger row.
- Record the runtime implementation's actual merge timestamp. Run
  `src/switch_review.py --assess-sandbox-fallback --since <actual-merge-timestamp>
  --ledger <actual-ledger> --json`; retain an explicitly unverified interim result
  and a final result after seven real days.

Blocking/drainable pair: successful current-head CI and review are required before
merge / focused branch validation, scope reduction and runtime follow-up remain
drainable while that merge gate is closed. No merge is performed by this salvage.
