Closes #581.

Prompt batch manifests now retain validator-rejected bodies and raw backend output alongside their existing verdicts. A rejected proposal survives validation in `rejected_proposal`; failed/withheld bodies use `NNN.failed.md` and raw output uses `NNN.raw.txt`, preserving exact UTF-8/CRLF/trailing whitespace. Valid-only publication remains `NNN.md`; each new manifest field is null for valid items. The validator and role-run admission are unchanged.

## Tasks

- [x] Preserve rejected proposals before clearing the accepted proposal.
- [x] Retain failed bodies, including valid bodies withheld after backend/recording failure, and raw output beside the manifest.
- [x] Add the named mixed-batch test; extend CLI and recording-failure controls.
- [x] Update the existing architecture and preserve all collection/skip ceilings.

## Validation

- Original source: 6 failed, 19 passed; corrected prompt batch:25passed.
- Six related suites:65passed plus6subtests, zero skips. Roles selftest and focused mypy pass; Ruff, Black and whitespace checks pass.
- Surgical removal of rejected-proposal retention makes the named test fail; changing only the failed-body filename to the published filename makes the CLI assertion fail. Byte-identical restoration passes each.
- Exact collection3299 = mergedmain3295 +4 new independently executed nodes; every existing skip/selftest/gate/mypy ceiling is preserved.
- Full local `python3 src/verify.py`:3296passed,3failed,0skipped,3299collected;104/104module selftests,all5gates and122-module mypy ratchet pass. The3failures are Gemini catalog availability guards; exact unchanged mergedbase34bfcff independently reproduces all3 and relevant source/test bytes match. Existing #578 runtime owner retains qualification; no model/pin/effort change or full-suite PASS claim. Fresh hosted exact-head checks and review remain required.

[Raw logs, mutations and SHA256 bindings](docs/verification/issue-581-failed-prompt-evidence/README.md) are committed.

## Remaining source acceptance

- [ ] After normal merge and execution-mirror sync, qualify the next real role-authored batch of at least five bodies: every failed first attempt must have a retained body or raw output, and the per-half first-pass rate must be computable from the manifest alone. The existing program owner checks the next audit/research batch or2026-10-27 source check.

Local controlled replay is not a production batch, deployment or future program acceptance. Keep/reopen source581 after merge until actual comparison and that remaining criterion are dispositioned.

<!-- pr-preamble:start -->
<!-- meta:issue:581 -->
> **Source:** Issue #581

Closes #581

<!-- pr-preamble:end -->

<!-- auto-status-summary:start -->
## Automated Status Summary
#### Scope
`write_prompt_batch` publishes valid bodies only, and its manifest records a failed item's errors but not its body. When a role-authored body fails the validator, nobody can tell whether the model left a section out or the validator is wrong. The Capability Program's first role batch (W1, 2026-10-11) lost exactly that: one of two bodies failed, the raw body was gone, and the W1 check could not separate the two causes. The closing plan of 2026-10-13 (row C3) scores role-prompt by "validator first-pass rate per half" and requires failed bodies to be retained. This is a current break, not latent fragility.

Evidence, re-read on `main` (`eef7c2c`) on 2026-10-14:
- `src/roles.py:2287-2289` — on any validator error the proposal is discarded (`proposal = None`), so the rejected `issue_body` is not in the item result; on the replay path (`proposal_json`, no backend) no other copy exists.
- `src/roles.py:2338` and `:2352` — the item result exposes `issue_body` only for a valid proposal, and keeps `raw_output` in memory only.
- `src/roles.py:2460-2479` — `write_prompt_batch` writes a body file only when `valid`; every manifest item holds `target`, `role_run_id`, `body_file`, `valid`, `errors` and `role_record_error` and nothing else. A body withheld because its role run failed to record (`:2463`) is lost the same way.
- `src/roles.py:4222-4226` — the CLI prints that manifest and returns 1 on any error; nothing else is persisted.
- Brain, role run `role:prompt:gemini:1791752288616878000` (the W1 failed item): `decision_source: baseline_dispatcher`, no proposal stored.
- `ARCHITECTURE.md:1937-1938` documents the manifest as holding "all validator verdicts", which is the part that works today.
- The W1.5 batch of 2026-10-13 (`prompt-batch:ee93654848f64ca188f386e81bcb552f`, three role-authored bodies) had to be run through a wrapper that copies each item's full result into a side file before the manifest writer drops it. All three bodies passed the validator, so nothing was lost this time; the first failure would have been.

<!-- Updated WORKFLOW_OUTPUTS.md context:start -->
## Context for Agent

### Related Issues/PRs
- [stranske/Orchestrator#575](https://github.com/stranske/Orchestrator/issues/575)
- [#416](https://github.com/stranske/Orchestrator/issues/416)
- [#526](https://github.com/stranske/Orchestrator/issues/526)
- [#527](https://github.com/stranske/Orchestrator/issues/527)
<!-- Updated WORKFLOW_OUTPUTS.md context:end -->

#### Tasks
- [x] `src/roles.py:2287-2289` — before `proposal = None`, keep the rejected proposal as `rejected_proposal` in the item result, and add the key to the result dict at `src/roles.py:2338-2353`.
- [x] `src/roles.py:2449-2483` (`write_prompt_batch`) — for every item with `valid` false, write `NNN.failed.md` holding the item's body when one exists (the rejected `issue_body`, or the valid body withheld by a recording failure) and `NNN.raw.txt` holding `raw_output` when it is not `None`; never write a failed body to the published `NNN.md` name.
- [x] `src/roles.py:2470-2479` — add `failed_body_file` and `raw_output_file` (a path or `null`) to every manifest item; a valid item carries `null` in both.
- [x] `tests/test_role_prompt_batch.py:265-303` (`test_cli_batch_writes_valid_bodies_and_invalid_item_verdicts`) — assert the invalid item's `failed_body_file` holds `Bad body` and that `002.md` still does not exist; `tests/test_role_prompt_batch.py:503-517` (`test_batch_recording_failure_withholds_body`) — assert the withheld body is retained under `failed_body_file` and `body_file` is still `None`.
- [x] `ARCHITECTURE.md:1937-1938` — state that the manifest also names each failed item's retained body and raw output files.

#### Acceptance criteria
- [x] Named test: `tests/test_role_prompt_batch.py::test_failed_body_and_raw_output_are_retained_in_the_manifest` — one valid item, one item rejected by the validator (`proposal("Bad body")` through `proposal_json`) and one item whose backend output is unparseable text: the manifest holds `failed_body_file` with `Bad body` for the second, `raw_output_file` with the unparseable text for the third, `valid` false and a non-empty `errors` for both, and `null` in both new keys for the valid item.
- [ ] `python3 -m pytest tests/test_role_prompt_batch.py -q` exits 0 with the two extended tests above passing.
- [x] Deliberate-break → revert: delete the `rejected_proposal` assignment at `src/roles.py:2289` so only `proposal = None` remains → the named test FAILS; then make `write_prompt_batch` write a failed body to `NNN.md` → the assertion at `tests/test_role_prompt_batch.py:303` FAILS; revert both.
- [ ] Smallest full test and check date: the next real role-authored batch of at least 5 bodies (the next audit or research-program filing batch, or the 2026-10-27 program check). Verdicts: retained equals failed means every first-try failure is diagnosable and the per-half first-pass rate is computable from the manifest alone; any gap names the item whose failure left no file.
- [ ] Pass number (closing-plan row C3, 2026-10-13): on that batch the query `python3 -c "import json,sys; m=json.load(open(sys.argv[1])); f=[i for i in m['items'] if not i['valid']]; print(sum(bool(i.get('failed_body_file') or i.get('raw_output_file')) for i in f), len(f))" <batch dir>/manifest.json` prints two equal integers, checked 2026-10-27.

<!-- auto-status-summary:end -->

<!-- This is an auto-generated comment: release notes by coderabbit.ai -->
## Summary by CodeRabbit

* **New Features**
  * Failed prompt-generation results now retain the authored prompt and raw backend output as separate evidence files, including when role recording fails or backend output is empty or unparseable. Accepted prompts continue to be published as before.
* **Documentation**
  * Updated guidance explains how batch results identify failed-body and raw-output evidence, how rejected proposals are distinguished from accepted prompts, and how failed and accepted outputs are recorded.
<!-- end of auto-generated comment: release notes by coderabbit.ai -->

## Opener routed review recovery — 2026-10-14

- [x] `src/roles.py::write_prompt_batch`: when an invalid backend-free dispatch_prompt has rejected_proposal.scoped_prompt, preserve its exact UTF-8 bytes in failed_body_file. Verified in five isolated backend-free rejected/valid replay batches; discussion_r4266549279 and discussion_r4266667328 are already resolved.
- [ ] `tests/test_role_prompt_batch.py`: add the rejected dispatch_prompt/scoped_prompt replay regression requested by discussion_r4266667328; run `python3 -m pytest tests/test_role_prompt_batch.py -q` without a provider call. Preserve the source issue's future real-batch acceptance as program-owned pending evidence; do not convert CI/review recovery into a human hold.

## Current validation — 2026-10-14

The scoped-prompt writer fix is present in recent commits. The replay regression now includes a valid neighboring dispatch item and checks its persisted body and null failure fields without adding collected test nodes. Five isolated backend-free production-function replay checks pass for Unicode/CRLF/trailing whitespace, empty and whitespace-only authored text, and malformed text. `python3 src/roles.py --selftest` and `git diff --check` pass.

The current interpreter has neither pytest nor Black, and PyPI is unreachable. The required pytest run and both Black commands cannot execute; no fresh pytest or formatting pass is claimed. The new Python test changes remain uncommitted under the mandatory formatting gate. The PR-body update was rejected by the connector because writes require approval while approval policy is `never`; this file is the prepared reconciliation. No real-batch qualification or human hold is asserted.
