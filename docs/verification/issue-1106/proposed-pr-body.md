### Keepalive: OFF
<!-- meta:issue:1106 -->

### Source Issue #1106: Establish owner-host role-redirect operational acceptance using the merged evidence tools

Base: main
Head: codex/issue-1106

Source: https://github.com/stranske/Orchestrator/issues/1106

<!-- auto-status-summary:start -->
## Automated Status Summary
#### Scope
The principal remaining work requires authentic owner-host state and an eligible real stall across the existing bounded cadence, rather than another focused tooling PR. Preserve the merged tools and historical limitations.
Source issue #1088; merged PR #1089 delivered: PR #1089 delivered provenance auditing, exact-revision verification, runtime observation and recovered-receipt checking tools with supporting controls. Its retained execution evidence honestly reports blocked tests and unavailable production observations; it did not complete real-stall operational acceptance.

<!-- Updated WORKFLOW_OUTPUTS.md context:start -->
## Context for Agent

### Related Issues/PRs
- [stranske/Orchestrator#1088](https://github.com/stranske/Orchestrator/issues/1088)
- [#1088](https://github.com/stranske/Orchestrator/issues/1088)
- [#1089](https://github.com/stranske/Orchestrator/issues/1089)
<!-- Updated WORKFLOW_OUTPUTS.md context:end -->

#### Tasks
- [x] Run the merged exact-revision verifier with the repository-selected Python or /opt/anaconda3/bin/python3 after confirming pytest is available; retain interpreter identity, revision, clean process results and actual output.
- [ ] Inspect retained November 3 owner-host artifacts for deployed flag, cadence, eligible candidates, refusals and lineage. Record blocking and drainable counts; use candidates 0 only for a verified empty population.
- [x] If November 3 evidence is unrecoverable, retain that limitation explicitly and distinguish any later observation; do not claim the historical criterion passed.
- [ ] Observe the next eligible real stall through the existing armed bounded cadence, preserving writer locks and the daily cap. Capture the candidate-to-apply-to-review-to-resolution chain and a documented control comparison.
- [x] Repair only a concrete failure printed by the real execution. Check collector compatibility on the actual Node runtime before treating an unavailable result as missing production state.
- [ ] Reconcile completion tracking with the actual evidence so the merged PR's checked inspection task and closure directive do not conceal unfinished acceptance.

#### Acceptance criteria
- [x] An identified merged revision has successful real named-test and focused-file executions plus authorized N/12 replay with N >= 9 and no process errors.
- [x] November 3 observations are supported by authentic retained evidence, or their unavailability remains explicit and the historical criterion remains unmet.
- [ ] One eligible real stall has an attributable bounded apply, linked switch-review acted_on > 0, measured stall-to-resolution and a documented control comparison.
- [x] Every receipt states its actual observation date; missing state remains unknown and later runs are never backdated.

<!-- auto-status-summary:end -->

Execution evidence: [issue-1106 receipts](https://github.com/stranske/Orchestrator/tree/codex/issue-1106/docs/verification/issue-1106). The retained exact-revision run passed 2 named tests, 25 focused tests and authorized 12/12 offline. A November 3 runner follow-up repaired the reproduced collector `EPERM` failure; 46 scoped Node tests pass. Authentic owner-host flag, cadence, populations, refusals and lineage remain unavailable. Blocking, drainable and candidates remain unknown. No real-stall apply, review, resolution or control comparison is established; historical and real-stall acceptance remain unmet. The merged PR #1089 inspection checkbox and closure directive do not establish those facts.

<details>

<summary>Full Issue Text</summary>



<!-- closer-core/v1 followup-of=stranske/Orchestrator#1088@1089 -->
## Why
The principal remaining work requires authentic owner-host state and an eligible real stall across the existing bounded cadence, rather than another focused tooling PR. Preserve the merged tools and historical limitations.
Source issue #1088; merged PR #1089 delivered: PR #1089 delivered provenance auditing, exact-revision verification, runtime observation and recovered-receipt checking tools with supporting controls. Its retained execution evidence honestly reports blocked tests and unavailable production observations; it did not complete real-stall operational acceptance.

## Scope
- Run the merged exact-revision verifier with the repository-selected Python or /opt/anaconda3/bin/python3 after confirming pytest is available; retain interpreter identity, revision, clean process results and actual output.
- Inspect retained November 3 owner-host artifacts for deployed flag, cadence, eligible candidates, refusals and lineage. Record blocking and drainable counts; use candidates 0 only for a verified empty population.
- If November 3 evidence is unrecoverable, retain that limitation explicitly and distinguish any later observation; do not claim the historical criterion passed.
- Observe the next eligible real stall through the existing armed bounded cadence, preserving writer locks and the daily cap. Capture the candidate-to-apply-to-review-to-resolution chain and a documented control comparison.
- Repair only a concrete failure printed by the real execution. Check collector compatibility on the actual Node runtime before treating an unavailable result as missing production state.
- Reconcile completion tracking with the actual evidence so the merged PR's checked inspection task and closure directive do not conceal unfinished acceptance.

## Non-Goals
- Do not redo work that PR #1089 already delivered.

## Tasks
- [x] Run the merged exact-revision verifier with the repository-selected Python or /opt/anaconda3/bin/python3 after confirming pytest is available; retain interpreter identity, revision, clean process results and actual output.
- [ ] Inspect retained November 3 owner-host artifacts for deployed flag, cadence, eligible candidates, refusals and lineage. Record blocking and drainable counts; use candidates 0 only for a verified empty population.
- [x] If November 3 evidence is unrecoverable, retain that limitation explicitly and distinguish any later observation; do not claim the historical criterion passed.
- [ ] Observe the next eligible real stall through the existing armed bounded cadence, preserving writer locks and the daily cap. Capture the candidate-to-apply-to-review-to-resolution chain and a documented control comparison.
- [x] Repair only a concrete failure printed by the real execution. Check collector compatibility on the actual Node runtime before treating an unavailable result as missing production state.
- [ ] Reconcile completion tracking with the actual evidence so the merged PR's checked inspection task and closure directive do not conceal unfinished acceptance.

## Acceptance Criteria
- [x] An identified merged revision has successful real named-test and focused-file executions plus authorized N/12 replay with N >= 9 and no process errors.
- [x] November 3 observations are supported by authentic retained evidence, or their unavailability remains explicit and the historical criterion remains unmet.
- [ ] One eligible real stall has an attributable bounded apply, linked switch-review acted_on > 0, measured stall-to-resolution and a documented control comparison.
- [x] Every receipt states its actual observation date; missing state remains unknown and later runs are never backdated.

## Implementation Notes
Verifier report (excerpt):

```
on tests use a stubbed Python (`/control/python`), so they do not show that the real pytest nodes and real offline replay (authorized N/12, N >= 9) exit zero on an identified merged revision. That proof depends on the sibling `issue-1088-merged-revision.json` artifact. I could not check whether it is a genuine run on the repository-selected interpreter.; Acceptance criterion 'Exact-revision focused execution exits zero and offline replay prints authorized N/12 with N >= 9' is unmet. Chunk 0's docs/verification/issue-1088-merged-revision.json is status 'blocked' for revision 8f596dc: both pytest invocations exit 1 with 'No module named pytest', so neither the two named tests nor the two focused files ran. Offline replay printed authorized 12/12, which is only half the criterion. Several executions also record exit_code 0 alongside error_code EPERM, an unresolved anomaly. scripts/verify_redirect_merged_revision.js (chunk 1) and its tests (chunk 2) are tooling only. Chunk 2's tests use a stubbed Python, so they do not prove a real run. No chunk contains a successful real execution.; Acceptance criterion 'November 3 is documented with actual eligible candidate and refusal evidence; a verified empty population is labeled candidates 0' is unmet. docs/verification/issue-1088-runtime-observation.json is status 'unavailable', with the corpus, plan, sweep, log and switch-review artifacts all missing. Blocking, drainable and candidates are null (unknown), not a verified 0. scripts/observe_redirect_runtime.js (chunk 1) and tests/test_observe_redirect_runtime.js provide capture tooling only. The armed flag, cadence, refusal and lineage inspection has no recorded result.; Acceptance criterion 'For an eligible real stall, the armed bounded path produces an attributable apply and switch-review acted_on > 0, with stall-to-resolution and a documented control comparison' has no implementation in any chunk. There is no attributable apply, acted_on > 0, resolution timing or control comparison. The tooling hard-codes operational_acceptance as 'not_established'. This is external operational evidence that has not been produced. Leaving it unestablished is honest, but the criterion remains unmet.; Task 'Recover private source-to-fixture bindings and any genuine October 20 replay receipt' is not fulfilled. The private corpus and the October 20 receipt were not recovered. The audit tooling and the recovered October 14 receipt only support the explicitly retained limitation. The 'or limitation retained' alternative covers the acceptance criterion, but the task stays open.; The PR body marks 'Inspect the November 3 armed flag, cadence, eligible candidate population, refusal records and action lineage' as [x]. The runtime-observation artifact shows everything unavailable, so the checkbox is unsupported by the evidence provided. The PR body also says 'Closes #1088' while acceptance is explicitly incomplete, so merging would auto-close an issue whose acceptance is not complete.; Minor quality and compatibility risks. docs/verification/issue-1088-provenance-audit.md is a long append-only session log that mixes tool documentation with transient status. scripts/observe_redirect_runtime.js uses fs.readdirSync recursive with entry.parentPath, which needs Node 20.12 or later and has no version check.; Evidence retrieval is incomplete: PR comments and workflow artifacts are marked unavailable or truncated, so these gaps cannot be called absent or resolved from that material.
```



</details>

—
PR created automatically to engage Codex.

<!-- pr-preamble:start -->
<!-- meta:issue:1106 -->
> **Source:** Issue #1106

Related to #1106; operational acceptance remains incomplete.

<!-- pr-preamble:end -->
