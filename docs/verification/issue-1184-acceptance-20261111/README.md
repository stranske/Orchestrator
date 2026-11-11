# Operational acceptance readback — 2026-11-11

Acceptance remains incomplete. No exporter defect was observed, no native producer
records were manufactured, and neither historical PR head was changed.

## Native PR reconciliation

| PR | Final head | Published merge | Native state |
|---|---|---|---|
| 1168 | `01e86ae7e4ef0e6a06193c916c8c780ca5cab052` | `a30b34ada04a30d04360867e14097a8c5b6d731c` | merged 2026-11-05T21:45:35Z |
| 989 | `85adde2614db7118d6e6d28dd11e5bc4c7611319` | `97647baf97085724af7cb8f684af2331f73fb612` | merged 2026-11-06T12:20:26Z |

The earlier unmerged reports describe historical observations. Native current
readback supersedes them. Routing PR 1168 through `merge_guard` is unnecessary:
the task conditions that action on the PR remaining open. No merge was attempted.

`native-readback.json` retains native PR metadata, verifier event bodies and merge
acknowledgments with their original URLs. A merge acknowledgment does not establish
`closer_coding_publication_io.retain_confirmed_result` custody or its
`coding_terminal_reference`.

## Native verifier and custody blockers

PR 1168's native verifier events bind its final head to its published merge.
Runs 42141057834 and 42142641060 report `incomplete`, with
`failure_cause=retained_provider_evaluation`. Run 42142328293 reports
`incomplete`, with `failure_cause=retained_revalidation_before_publication`.
Workflow success for 42141057834 and 42142641060 is not a corpus decision.
No `verifier-corpus-decision/v1` marker was found in the returned PR 1168
comment page. PR 984 events and skipped runs were not credited.

This run could not dispatch the native workflow: shell GitHub access fails,
and the available connector mutations require approval under a `never`
approval policy. No new verifier run or decision is claimed.

The current chain command exits **2** because
`/home/runner/.codex/orchestrator/closer-core/completions/stranske_orchestrator_988_989.json`
is absent. The exact failing read is `closer_csv_completion.read_completion`
calling `_bytes(path)`; `closer_csv_completion.main` reports the missing receipt.
This is unavailable custody, not an established mismatch with producer records
or a demonstrated checker defect. Neither the retired publication module nor
its native custody is present in this checkout. The earlier worker's original
terminal log cannot be reconstructed from merge comments or keepalive receipts.

## Local verification and source change

`local-checks.json` records the selected interpreter and exit statuses. Anaconda
is unavailable, so the suite-selected Python 3.14.10 interpreter was used.
Full stdout and stderr are retained separately for each command.

- Corrected contract: **0**, `contract ok`.
- Isolated partial revision `0507d28acf68d6bf21c68138d18d56cd01968827`:
  **1**, `AssertionError: CSV contract is not implemented`. The regression verifies
  the embedded source's Git blob hash and compares the historical revision when local.
- Current `--chain-evidence`: **2**, missing native receipt.
- `--self-check`: **0**, `chain-evidence self-check ok`.

Inspection of the complete parsed module and entry point confirms explicit
standard-library contract execution. Module-level statements define imports,
constants and functions; default execution calls `run_contract` and never calls
the evidence readers. The legacy reader requires `--chain-evidence --legacy`.
The fixture sets `__test__ = False`, defines no `test_*` functions and does not
match pytest's default module discovery pattern. `pyproject.toml` does not alter
that pattern. Pytest collection could not be executed in this runner.

The existing entry-point regression now exercises the current chain command
against an empty temporary custody location while rejecting network/subprocess
and legacy evidence reads. It verifies exit 2, an incomplete report identifying
the missing native receipt, no created custody, and restoration of `sys.path`.
Both focused regression functions pass when invoked directly using standard-library
fixture adapters. This does not substitute for a pytest run.

`pytest -m "not slow"` could not run because pytest is absent. Black formatting
and the required repository-wide Black check could not run because Black is
absent. Dependency installation failed. The test change is **uncommitted**:
the required formatting gate forbids committing until Black passes.
`git diff --check` passes. No protected files were edited.

## Checklist handoff

Recent commits `f464d46a`, `9e87c7ab`, and `07aa1c87` added the corrected/partial
regression, preserved its pinned partial source, and restricted legacy evidence
entry to explicit chain opt-in. Native readback plus the local checks support
checking the PR-state task and the first two acceptance criteria. The conditional
merge task is satisfied as not applicable because PR 1168 is already merged.

`pending-pr-body.md` contains the proposed reconciled body. It was **not applied**:
the PR-body update, blocker comment and `needs-human` label were each rejected
with `MCP tool call requires approval, but approval policy is never`.
PR 1348 was read back open and ready for review (`draft=false`); no PR state
mutation succeeded. The verifier decision, stored worker/publication custody,
and overall lifecycle acceptance remain unchecked.
