# Issue #435 — merged gitignore delivery evidence

Parent issue: [stranske/Orchestrator#435](https://github.com/stranske/Orchestrator/issues/435).

Rails landed in PR [#502](https://github.com/stranske/Orchestrator/pull/502) (`campaigns/gitignore-caches-2026-10.json`, `codemod_lane --file-targets`, `range_lane_rollout --campaign`). Its merge metadata is retained in [rails.json](rails.json). GitHub issue, PR and exact merge-parent comparisons confirm all six filed target issues are closed with merged delivery PRs:

| Repository receipt | Closed issue | Merged PR |
| --- | --- | --- |
| [stranske/Counter_Risk](Counter_Risk.json) | [#1145](https://github.com/stranske/Counter_Risk/issues/1145) | [#1146](https://github.com/stranske/Counter_Risk/pull/1146) |
| [stranske/Manager-Database](Manager-Database.json) | [#1755](https://github.com/stranske/Manager-Database/issues/1755) | [#1756](https://github.com/stranske/Manager-Database/pull/1756) |
| [stranske/Inv-Man-Intake](Inv-Man-Intake.json) | [#1005](https://github.com/stranske/Inv-Man-Intake/issues/1005) | [#1006](https://github.com/stranske/Inv-Man-Intake/pull/1006) |
| [stranske/learning-management-system](learning-management-system.json) | [#748](https://github.com/stranske/learning-management-system/issues/748) | [#749](https://github.com/stranske/learning-management-system/pull/749) |
| [stranske/trip-planner](trip-planner.json) | [#1887](https://github.com/stranske/trip-planner/issues/1887) | [#1888](https://github.com/stranske/trip-planner/pull/1888) |
| [stranske/Pension-Data](Pension-Data.json) | [#948](https://github.com/stranske/Pension-Data/issues/948) | [#950](https://github.com/stranske/Pension-Data/pull/950) |

Historical acceptance: `python3 -m pytest tests/test_codemod_campaign.py -q` — **23 passed**, reported by the original closure commit `49fe4ccd03b52b4e3aa0183b07affd96fea785a0` on 2026-10-07. This runner could not repeat it: `python3 -m pytest tests/test_codemod_campaign.py -q -m 'not slow'` exits 1 with `No module named pytest`; package installation is unavailable. The historical result is retained with that provenance in [receipt.json](receipt.json).

Each delivery receipt pins the original PR head, merge commit, merge time, issue disposition, exact changed-file statistics and GitHub blob hashes. It retains the original `.gitignore` and the exact appended text, allowing the merged bytes to be reconstructed and independently checked against the Git blob hash. Manager-Database already had the effective root rule `/.coverage`; its delivery added only the two cache entries. The learning-management-system delivery also added a regression in `tests/test_repo_hygiene.py`; its receipt preserves that additional changed file. This is a scope exception to the campaign's "Only .gitignore changes" constraint, not a compliant .gitignore-only delivery. The added test protects the ignore behavior, but its utility does not waive the constraint. The receipt records this exception explicitly; campaign acceptance remains incomplete.

Receipt verification: `node --test --test-isolation=none tests/test_codemod_campaign_receipts.js` — **8 passed**, zero failures or skips. The tests check all six target identities against the versioned campaign, verify merged metadata and blob identities, and run Git against the reconstructed ignore files to require all five effective probe paths. The receipt tests supplement the historical Python acceptance result.

[receipt-controls.log](receipt-controls.log) retains four private-copy rejection controls: a missing target, an unmerged child, altered original bytes and a fabricated measured cost each fail the receipt tests. Restoring the original receipts returns all eight tests to green; no checkout evidence was mutated by the controls.

Runtime ledger `$ORCH_STATE_DIR/capability-program/codemod-campaign.json` is populated by `range_lane_rollout --record-campaign` on the operator host. Delivery completion does not establish a live range-dispatch trial, whole-delivery cost or time-based durability. Brain cost/durability remain UNKNOWN/null in these receipts; the attribution follow-up remains [PR #513](https://github.com/stranske/Orchestrator/pull/513).

Round task status:

- [x] Retain and verify per-repository merged delivery receipts for all six filed targets.
- [x] Independent opener replay: `/opt/anaconda3/bin/python3 -m pytest tests/test_codemod_campaign.py -q -o addopts=` — 23 passed on 2026-10-07. Existing Node CLI/receipt suite — 13 passed, zero failures or skips. Earlier runner limitations above remain historical.
- [ ] Complete live range-dispatch, authentic whole-delivery cost and durability evidence, and reconcile the recorded scope exception before parent #435 can close.

The [delivery disposition note](closure-note.md) preserves the historical GitHub write rejection and the current delivery/measurement split for the receiving closer. This PR references #435 without closing it.
