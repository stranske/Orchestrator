Tracks #1184

Salvages the CSV contract regression and its shard registration from #1185. The test runs the existing verifier against the corrected fixture and against an isolated copy of `csv_export.py` pinned to `0507d28acf68d6bf21c68138d18d56cd01968827`. It honors a selected virtual environment and uses the Anaconda interpreter when available outside one.

## Verified on November 11, 2026

- The corrected verifier printed `contract ok` and exited 0. The pinned partial revision raised `AssertionError: CSV contract is not implemented` and exited 1. The focused regression passed.
- The standalone verifier uses the standard library, calls its assertions explicitly in `main()`, and falls outside default pytest discovery. Default execution performs no evidence collection. The explicit `--chain-evidence --legacy` flag also reaches a historical evidence reader.
- GitHub reports both PRs merged. #1168's head is `01e86ae7e4ef0e6a06193c916c8c780ca5cab052` and its published merge is `a30b34ada04a30d04360867e14097a8c5b6d731c`. #989's head is `85adde2614db7118d6e6d28dd11e5bc4c7611319` and its published merge is `97647baf97085724af7cb8f684af2331f73fb612`.
- `--chain-evidence` exited 0 and reported runtime acceptance for #989 with `acceptance_authority=false`. The full stdout is retained in `chain-evidence-1348-continuation.stdout.txt`. [Native publication acknowledgment](https://github.com/stranske/Orchestrator/pull/989#issuecomment-6684682838)

## Outstanding operational evidence for #1168

- GitHub reports comparison run `42142641060`, attempt `1`, completed successfully. The exact event for PR #1168 is still `incomplete` with `retained_provider_evaluation` and `retryable=false`. The earlier evaluate-mode result is superseded. Artifact `13468552385` downloaded successfully and its ZIP matched GitHub's SHA-256 `2ebfd872ed901d067ddbac7990709aa23d7d46d9c03d4150e9fe091bb21dfcba`. The retained `comparison.json` matched the execution receipt's SHA-256 `3c359a5f4eb823a7d3e60da3b31a4a9b27440a31f7ce2bb5484c1a23e768cadc`. The report is `UNVERIFIABLE_INPUT`. The existing producer regression records `publishRetainedReport: Recovery report lacks one admitted terminal decision` for this report. Native comments contain zero corpus decisions. Production dispatch remains controller-owned and the dispatched seat has no callable controller recovery route. [Run](https://github.com/stranske/Orchestrator/actions/runs/42142641060/attempts/1), [native event](https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6674054696)
- Native PR, `refs/pull/1168/head`, and commit reads agree on the final corrective head. The retained worker log establishes pushes of ancestors. The first missing custody object is the original `publication-result-<sha256>.json` from `retain_confirmed_result` for the final head. Its `provenance.coding_terminal_reference` and confirmed push therefore remain unverified. [Native merge acknowledgment](https://github.com/stranske/Orchestrator/pull/1168#issuecomment-6670373009)

<!-- pr-preamble:start -->
<!-- meta:issue:1184 -->
> **Source:** Issue #1184

Tracks #1184

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
- [x] Route PR 1168's unchanged head through the existing authorized `merge_guard` path if PR 1168 remains open and after the review floor expires; preserve all review and merge gates and report the first remaining blocker. (Verified not applicable: already merged at a30b34ada04a30d04360867e14097a8c5b6d731c.)
- [ ] Run the existing native `Agents Verifier` workflow to obtain and retain a `verifier-corpus-decision/v1` bound to PR 1168's final head and published merge; do not substitute skipped runs or PR 984 events.
- [ ] Inspect `closer_coding_publication_io.retain_confirmed_result` custody and its `coding_terminal_reference` for the actual corrective delivery. Trace the worker's produced commit through native publication readback, including an earlier-started worker if it produced that commit; do not fabricate records or require a redundant worker solely to start on its own output commit.
- [x] Run `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py --chain-evidence` and retain its full output. It exits 0 for #989. The native #1168 evidence is tracked below.

#### Acceptance criteria
- [x] `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py`, using the repository-selected interpreter when applicable, exits zero on corrected code and nonzero with a contract AssertionError on the partial implementation from revision 0507d28acf68d6bf21c68138d18d56cd01968827.
- [x] Inspection of the complete `verify_csv_contract.py` entry point and repository collection configuration confirms explicit standard-library contract execution and no default network evidence collection; network or subprocess evidence collection is reached only through `--chain-evidence`.
- [ ] Native readback records PR 1168's final corrective head and published merge SHA, and a native `verifier-corpus-decision/v1` from `Agents Verifier` is bound to both; skipped runs and PR 984 events are excluded as evidence for that head.
- [ ] Worker terminal and stored `closer_coding_publication_io.retain_confirmed_result` records, including `coding_terminal_reference`, trace the worker's produced commit to the corrective delivery through native publication readback; keepalive receipts alone do not satisfy publication.
- [ ] `/opt/anaconda3/bin/python3 tests/fixtures/closer_real_csv_canary/v1/verify_csv_contract.py --chain-evidence` exits zero, or any mismatch with native producer records is reported as a checker defect with the exact rejecting function identified.
- [ ] Existing fixture work in `tests/fixtures/closer_real_csv_canary/v1/` and visible test labeling are retained; any repair commit addresses a recorded failing acceptance check.

<!-- auto-status-summary:end -->
