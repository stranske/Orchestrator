Closes #1184

Salvages the CSV contract regression and its shard registration from #1185. The test runs the existing verifier against the corrected fixture and against an isolated copy of `csv_export.py` pinned to `0507d28acf68d6bf21c68138d18d56cd01968827`. It honors a selected virtual environment and uses the Anaconda interpreter when available outside one.

## Verified on November 11, 2026

- The corrected verifier printed `contract ok` and exited 0. The pinned partial revision raised `AssertionError: CSV contract is not implemented` and exited 1. The focused regression passed.
- The standalone verifier uses the standard library, calls its assertions explicitly in `main()`, and falls outside default pytest discovery. Default execution performs no evidence collection. The explicit `--legacy-chain-evidence` flag also reaches a historical evidence reader.
- GitHub reports both PRs merged. #1168's head is `01e86ae7e4ef0e6a06193c916c8c780ca5cab052` and its published merge is `a30b34ada04a30d04360867e14097a8c5b6d731c`. #989's head is `85adde2614db7118d6e6d28dd11e5bc4c7611319` and its published merge is `97647baf97085724af7cb8f684af2331f73fb612`.
- `--chain-evidence` exited 0 and verified the retained worker, publication and runtime acceptance for #989. The current reader targets #989. [Native publication acknowledgment](https://github.com/stranske/Orchestrator/pull/989#issuecomment-6684682838)

## Outstanding operational evidence for #1168

- Three earlier Agents Verifier runs bound to the final head and published merge ended with incomplete event states. No `verifier-corpus-decision/v1` was found. This dispatched agent's attempt to run Agents Verifier on `main` was refused by its GitHub CLI guard. [Evaluation](https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6673821163), [revalidation](https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6673968504), [comparison](https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6674054696)
- The retained #1168 worker log records pushes of two ancestors of the final corrective head. GitHub confirms publication of the final head, but the accessible records did not establish which worker produced that commit or supply the stored `retain_confirmed_result` record and `coding_terminal_reference`. [Native merge acknowledgment](https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6670373009)

<!-- pr-preamble:start -->
<!-- meta:issue:1184 -->
> **Source:** Issue #1184

Closes #1184

<!-- pr-preamble:end -->

<!-- auto-status-summary:start -->
## Automated Status Summary
#### Scope
The remaining work is an operational acceptance run spanning existing PR state, native verification and worker/publication custody. No concrete CSV defect justifies another implementation PR.
Source issue #983; merged PR #1168 delivered: The supplied corrective diff implements the CSV contract while preserving JSON and unsupported-format behavior, and adds explicit contract assertions plus chain-evidence reporting. It does not establish successful completion of the controlled Closer lifecycle.

<!-- Updated WORKFLOW_OUTPUTS.md context:start -->
## Context for Agent

### Related Issues/PRs
- [stranske/Orchestrator#983](https://github.com/stranske/Orchestrator/issues/983)
- [#983](https://github.com/stranske/Orchestrator/issues/983)
- [#1168](https://github.com/stranske/Orchestrator/issues/1168)
- [#1185](https://github.com/stranske/Orchestrator/issues/1185)
<!-- Updated WORKFLOW_OUTPUTS.md context:end -->

#### Tasks
- [x] Read native state for `stranske/Orchestrator` PRs 1168 and 989 and record their current heads and published merge SHAs; reconcile the contradictory merged/unmerged reports without changing either head.
- [x] Run `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py` using the repository-selected interpreter when applicable. In an isolated scratch copy, run the same verifier against `csv_export.py` from partial revision 0507d28acf68d6bf21c68138d18d56cd01968827 and retain the contract AssertionError and exit status.
- [x] Inspect the complete `tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py` entry point and repository collection configuration to confirm the default command runs contract assertions explicitly and reaches network or subprocess evidence collection only through `--chain-evidence`.
- [x] Route PR 1168's unchanged head through the existing authorized `merge_guard` path if PR 1168 remains open and after the review floor expires; preserve all review and merge gates and report the first remaining blocker.
- [ ] Run the existing native `Agents Verifier` workflow to obtain and retain a `verifier-corpus-decision/v1` bound to PR 1168's final head and published merge; do not substitute skipped runs or PR 984 events.
- [ ] Inspect `closer_coding_publication_io.retain_confirmed_result` custody and its `coding_terminal_reference` for the actual corrective delivery. Trace the worker's produced commit through native publication readback, including an earlier-started worker if it produced that commit; do not fabricate records or require a redundant worker solely to start on its own output commit.
- [x] Run `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py --chain-evidence` and retain its full output alongside genuine native verifier and publication records.

#### Acceptance criteria
- [x] `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py`, using the repository-selected interpreter when applicable, exits zero on corrected code and nonzero with a contract AssertionError on the partial implementation from revision 0507d28acf68d6bf21c68138d18d56cd01968827.
- [x] Inspection of the complete `verify_csv_contract.py` entry point and repository collection configuration confirms explicit standard-library contract execution and no default network evidence collection; network or subprocess evidence collection is reached only through `--chain-evidence`.
- [ ] Native readback records PR 1168's final corrective head and published merge SHA, and a native `verifier-corpus-decision/v1` from `Agents Verifier` is bound to both; skipped runs and PR 984 events are excluded as evidence for that head.
- [ ] Worker terminal and stored `closer_coding_publication_io.retain_confirmed_result` records, including `coding_terminal_reference`, trace the worker's produced commit to the corrective delivery through native publication readback; keepalive receipts alone do not satisfy publication.
- [ ] `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py --chain-evidence` exits zero, or any mismatch with native producer records is reported as a checker defect with the exact rejecting function identified.
- [ ] Existing fixture work in `tests/fixtures/closer_real_csv_canary/v1/` and visible test labeling are retained; any repair commit addresses a recorded failing acceptance check.

<!-- auto-status-summary:end -->
