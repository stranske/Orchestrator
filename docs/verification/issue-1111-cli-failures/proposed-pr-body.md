### Keepalive: OFF
<!-- meta:issue:1111 -->

### Source Issue #1111: Recover authenticated adjudicator inputs and complete operational acceptance after #1105

Base: main
Head: codex/issue-1111

Source: https://github.com/stranske/Orchestrator/issues/1111

<!-- auto-status-summary:start -->
## Automated Status Summary
#### Scope
The remaining work requires historical input recovery, real execution, safety experiments, failure diagnosis and external metadata reconciliation; it exceeds one focused repair to the merged tooling.
Source issue #1104; merged PR #1105 delivered: PR #1105 fixed ambiguous status-marker validation, added supplied-manifest evidence-gap export, strengthened inventory filtering and retained focused regression evidence. It recovered the named acceptance commands and documented missing operational inputs without claiming operational acceptance.

<!-- Updated WORKFLOW_OUTPUTS.md context:start -->
## Context for Agent

### Related Issues/PRs
- [stranske/Orchestrator#1104](https://github.com/stranske/Orchestrator/issues/1104)
- [#1104](https://github.com/stranske/Orchestrator/issues/1104)
- [#1105](https://github.com/stranske/Orchestrator/issues/1105)
- [#1074](https://github.com/stranske/Orchestrator/issues/1074)
- [#1075](https://github.com/stranske/Orchestrator/issues/1075)
- [#1091](https://github.com/stranske/Orchestrator/issues/1091)
- [#1092](https://github.com/stranske/Orchestrator/issues/1092)
<!-- Updated WORKFLOW_OUTPUTS.md context:end -->

#### Tasks
- [ ] Retain the merged tooling; recover authenticated original cohort evidence and the permitted persisted inventory without accessing owner-excluded repositories.
- [ ] Persist a result or specific evidence gap for every authenticated requested case, linked to revision, role-run provenance and measured or explicitly unknown cost.
- [ ] On one recorded revision, run the three recovered tests in tests/test_adjudicator_retro.py, perform both specified mutations, capture the expected named failures, restore byte-identically and rerun successfully using the selected repository interpreter.
- [ ] Capture a real contested closer observation with target, revision, persisted shadow role record and authoritative outcomes before and after.
- [ ] Reconcile the saved summary and actual switch-review output to persisted case, role-run and cost records.
- [ ] Identify all 16 retained CLI failures and supply individual evidence-backed dispositions and relevant passing checks.
- [ ] Correct the premature completion claim and establish an explicit successor handoff; reconcile #1074/#1075/#1091/#1092 and #1104/#1105 metadata through the authorized publication path, then retain fresh readbacks.

#### Acceptance criteria
- [ ] Authenticated provenance and reconciliation account for every requested case and discrepancy; missing identities remain an explicit acceptance failure rather than a substituted cohort.
- [ ] One revision has retained three named passes, two expected mutation failures, matching restoration hashes and passing reruns.
- [ ] A real contested record links to a persisted shadow role record and byte- or field-verified unchanged authoritative outcomes.
- [ ] Saved summary and switch-review totals reconcile with persisted records, with unknown costs explicitly identified.
- [ ] Exactly 16 retained failures have individually supported dispositions and appropriate passing checks.
- [ ] Fresh remote readbacks show accurate completion boundaries and durable successor links for unfinished work.

<!-- auto-status-summary:end -->

<details>

<summary>Full Issue Text</summary>



<!-- closer-core/v1 followup-of=stranske/Orchestrator#1104@1105 -->
## Why
The remaining work requires historical input recovery, real execution, safety experiments, failure diagnosis and external metadata reconciliation; it exceeds one focused repair to the merged tooling.
Source issue #1104; merged PR #1105 delivered: PR #1105 fixed ambiguous status-marker validation, added supplied-manifest evidence-gap export, strengthened inventory filtering and retained focused regression evidence. It recovered the named acceptance commands and documented missing operational inputs without claiming operational acceptance.

## Scope
- Retain the merged tooling; recover authenticated original cohort evidence and the permitted persisted inventory without accessing owner-excluded repositories.
- Persist a result or specific evidence gap for every authenticated requested case, linked to revision, role-run provenance and measured or explicitly unknown cost.
- On one recorded revision, run the three recovered tests in tests/test_adjudicator_retro.py, perform both specified mutations, capture the expected named failures, restore byte-identically and rerun successfully using the selected repository interpreter.
- Capture a real contested closer observation with target, revision, persisted shadow role record and authoritative outcomes before and after.
- Reconcile the saved summary and actual switch-review output to persisted case, role-run and cost records.
- Identify all 16 retained CLI failures and supply individual evidence-backed dispositions and relevant passing checks.
- Correct the premature completion claim and establish an explicit successor handoff; reconcile #1074/#1075/#1091/#1092 and #1104/#1105 metadata through the authorized publication path, then retain fresh readbacks.

## Non-Goals
- Do not redo work that PR #1105 already delivered.

## Tasks
- [ ] Retain the merged tooling; recover authenticated original cohort evidence and the permitted persisted inventory without accessing owner-excluded repositories.
- [ ] Persist a result or specific evidence gap for every authenticated requested case, linked to revision, role-run provenance and measured or explicitly unknown cost.
- [ ] On one recorded revision, run the three recovered tests in tests/test_adjudicator_retro.py, perform both specified mutations, capture the expected named failures, restore byte-identically and rerun successfully using the selected repository interpreter.
- [ ] Capture a real contested closer observation with target, revision, persisted shadow role record and authoritative outcomes before and after.
- [ ] Reconcile the saved summary and actual switch-review output to persisted case, role-run and cost records.
- [ ] Identify all 16 retained CLI failures and supply individual evidence-backed dispositions and relevant passing checks.
- [ ] Correct the premature completion claim and establish an explicit successor handoff; reconcile #1074/#1075/#1091/#1092 and #1104/#1105 metadata through the authorized publication path, then retain fresh readbacks.

## Acceptance Criteria
- [ ] Authenticated provenance and reconciliation account for every requested case and discrepancy; missing identities remain an explicit acceptance failure rather than a substituted cohort.
- [ ] One revision has retained three named passes, two expected mutation failures, matching restoration hashes and passing reruns.
- [ ] A real contested record links to a persisted shadow role record and byte- or field-verified unchanged authoritative outcomes.
- [ ] Saved summary and switch-review totals reconcile with persisted records, with unknown costs explicitly identified.
- [ ] Exactly 16 retained failures have individually supported dispositions and appropriate passing checks.
- [ ] Fresh remote readbacks show accurate completion boundaries and durable successor links for unfinished work.

## Implementation Notes
Verifier report (excerpt):

```
ata and handoffs) has no evidence in these files. The generator remains proposal-only. This is external evidence required rather than a standalone code defect, but it remains unproven.; Because the criteria are not met, the PR preamble line `Closes #1104` was inconsistent with the PR's own documented state ('Only the first task is complete'). Issue #1104 shows as CLOSED, which is a premature-closure risk. The verification doc is candid about the shortfall.; Acceptance criterion 1 (authenticated manifest and reconciliation) is not implemented. reconciliation.json records 0 of 71 original identities recovered. runner-inventory.json reports status 'unavailable' with null counts. source-428.json is captured remote issue text, and its own boundary field says it is not an authenticated cohort manifest. scripts/export_adjudicator_case_gaps.js and scripts/export_adjudicator_inventory.js, with tests/test_adjudicator_case_gaps.js and tests/test_adjudicator_inventory.js, supply the tooling. They have not been run against an authenticated original cohort, and requested_cohort_verified remains false.; Acceptance criterion 2 (one recorded revision with three named test passes, both mutation failures, byte-identical restoration and passing reruns) is not met. named-acceptance-attempt.log contains only 'No module named pytest'. validation.json lists the named pytest acceptance under not_run. The three test names were recovered, but no run, mutation or restoration evidence exists in the PR. The mutation transcripts appear only in a quoted comment on the source issue.; Acceptance criterion 3 (a real contested closer observation) is not met. No chunk provides a target, revision, persisted shadow role record or authoritative outcomes before and after. validation.json lists the observation as not run.; Acceptance criterion 4 (reconciling the saved summary and switch-review output against persisted case, role-run and cost records) is not met. The case-gaps exporter marks every shadow revision, result, role-run provenance and cost as UNKNOWN. It is not run against any original cohort. The saved 37-of-122 summary and the actual switch-review output are not reconciled with persisted records.; Acceptance criterion 5 (individual dispositions of all 16 retained CLI failures) is not met. The full CLI suite was not run, and no per-failure disposition exists. Only the 4 'retro saves Gate gaps' tests and the inventory tests were re-run.; Acceptance criterion 6 (fresh remote readbacks of #1074/#1075/#1091/#1092 showing consistent completion claims and handoffs) has no evidence. The README states connector writes were rejected, and the generator remains proposal-only. This depends on external publication evidence, but the readbacks are absent.; Premature-closure inconsistency: the PR preamble says 'Closes #1104' and issue #1104 is CLOSED, yet the PR's own documents say only the first task is complete. Five tasks and all six acceptance criteria remain unchecked.; Evidence-quality limits: the README refers to /tmp delivery paths and /tmp/issue-1104-reconciled-pr-body.md, none of which can be retrieved. evidence.log is mostly sqlite ResourceWarning noise, which suggests unclosed connections that were not addressed. PR comments and most workflow artifacts were unavailable for inspection. The CI run was still in progress.; Not supported by the evidence: the CI conclusion and the contents of the artifacts that could not be extracted. These do not supply any missing criterion.
```



</details>

—
PR created automatically to engage Codex.

Acceptance remains incomplete: all 26 checkboxes are unchecked. This round repairs CLI test drift and retains individual diagnoses for the original 16 failures. Two verifier-finding source failures remain. The original cohort, permitted Brain, named pytest safety controls, real closer observation, operational cost reconciliation and authorized metadata readbacks remain required. Successor: https://github.com/stranske/Orchestrator/issues/1111

<!-- pr-preamble:start -->
<!-- meta:issue:1111 -->
> **Source:** Issue #1111

References #1111

<!-- pr-preamble:end -->
