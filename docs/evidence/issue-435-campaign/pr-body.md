## Summary

- Adds `docs/evidence/issue-435-campaign/` with per-repo merged delivery receipts for all six filed targets (issues #1145, #1755, #1005, #748, #1887, #948).
- Retains the historical 23-pass pytest report and adds five passing CLI regression tests for campaign validation, filing, linking, retries, and rollout guards.
- Records merged child delivery after rails in #502; live rollout evidence, named pytest verification, and Brain cost/durability follow-up remain incomplete.

Refs #435

## Test plan

- [x] `node --test --test-isolation=none tests/test_codemod_campaign_cli.js tests/test_codemod_campaign_receipts.js` — 13 passed.
- [x] `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_codemod_campaign_*py' -q` — 13 passed.
- [x] `python3 src/codemod_lane.py --validate campaigns/gitignore-caches-2026-10.json` — Validation PASSED.
- [x] `python3 -m pytest tests/test_codemod_campaign.py -q`

<!-- pr-preamble:start -->
<!-- meta:issue:435 -->
> **Source:** Issue #435

Refs #435

<!-- pr-preamble:end -->

<!-- auto-status-summary:start -->
## Automated Status Summary

#### Scope

_Scope section missing from source issue._

#### Tasks

- [x] `campaigns/gitignore-caches-2026-10.json` — the campaign: the five entries, `tool: custom`, `pr_strategy: per_repo`, `repos` = the six above, a `delegate_prompt` that adds only missing lines and never removes any; `python3 src/codemod_lane.py --validate` green.
- [x] `src/codemod_lane.py` — `--file-targets <campaign.json>`: one issue per target repo labelled `codemod`, titled `[P2] Ignore tool caches and coverage artifacts (fleet codemod campaign)`, body = the campaign's per-repo plan; print the issue URLs and link them from this issue.
- [ ] `src/range_lane_rollout.py` — a `--campaign <json>` input that lists exactly those issues as the backlog (no discovery), previews the dispatches, and with the owner's one-off window (`ORCH_RANGE_LANE_ROLLOUT=1 --apply --confirm-rollout`) dispatches them through the router; prints `campaign: repos N, dispatched D, merged M`.
- [ ] `$ORCH_STATE_DIR/capability-program/codemod-campaign.json` — per-repo outcome (merged, durable, cost) for the program's measure.

#### Acceptance criteria

- [x] Named test: `tests/test_codemod_campaign.py::test_the_campaign_validates_and_targets_only_missing_entries`, `::test_rollout_with_a_campaign_input_previews_exactly_those_targets_and_refuses_without_the_window`.
- [ ] Deliberate-break → revert: let the delegate prompt remove an existing ignore line → first test FAILS (the validator asserts add-only); dispatch without the window → second FAILS; revert.

<!-- auto-status-summary:end -->

Reconciliation: campaign configuration and filing implementation are verified. All six filed issues retain the required title and codemod label; the parent links them in [the original target receipts comment](https://github.com/stranske/Orchestrator/issues/435#issuecomment-6024002384). Independent opener replay on 2026-10-07 ran `/opt/anaconda3/bin/python3 -m pytest tests/test_codemod_campaign.py -q -o addopts=`: 23 passed, including the two named acceptance tests. Earlier runner inability to load pytest is historical. The new CLI tests exercise the real issue-format subprocess with a local gh fixture and verify issue titles, labels, per-repo missing-entry plans, printed URLs, parent links, retries and rollout guards. They do not substitute for a live dispatch trial. The named Python tests passed independently; the deliberate-break acceptance below remains unchecked by this documentation correction. Total cost and durability remain unknown.

Earlier connector writes were rejected by its approval boundary. This PR retains a non-closing reference to #435. The learning-management-system delivery changed `tests/test_repo_hygiene.py` as well as `.gitignore`; its explicit scope exception is preserved in the receipt and is not claimed compliant with the campaign's .gitignore-only constraint. Live range dispatch, authentic whole-delivery cost, durability, and scope-exception reconciliation remain incomplete.
