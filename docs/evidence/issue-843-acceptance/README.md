# Issue #843 acceptance evidence (closer repair)

Generated: 2026-11-04T00:10:36.264862+00:00
Git HEAD: `8028a57556612e87574b2b7af628de60ef042613`
Python: `/opt/anaconda3/bin/python3`

## Reproducible commands

Focused baseline (25 added tests):

```bash
/opt/anaconda3/bin/python3 -m pytest -q tests/test_issue843_ranked_coverage.py tests/test_range_lane_rollout_coverage_gaps.py
```

Full acceptance runner (baseline + deliberate mutations + receipts):

```bash
/opt/anaconda3/bin/python3 scripts/run_issue843_acceptance.py --write-report
```

Combined coverage (CI-equivalent, slow):

```bash
ORCH_STATE_DIR="$(mktemp -d)" ORCH_LOCAL_RUNTIME="$(mktemp -d)" \
  python3 src/verify.py --coverage
```

CI uses sharded `python3 src/verify.py --pytest-shard i/6 --coverage` then
`python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag` on pull requests
(see `.github/workflows/ci.yml`). Branch coverage is included in Gate combined metrics
(`branch=false` in pytest-cov omits branches; this repository's Gate uses combined statement+branch).

## PR #845 recovered receipts (not absent when comments are unreachable)

{
  "pr": 845,
  "merge_commit": "2b88a6d2b1227e16d5e30367235304d79545eefe",
  "parent_base": "836461baee0e7c8befcd831ee02852775b574084",
  "pr_head": "2b96ffc13a337a920d4c4201f01a491998a92117",
  "gate_ci_run_pr_head": "https://github.com/stranske/Orchestrator/actions/runs/39505581812",
  "gate_coverage_baseline_run": "https://github.com/stranske/Orchestrator/actions/runs/39496947363",
  "merge_ci_run": "https://github.com/stranske/Orchestrator/actions/runs/39512253651",
  "documented_break_reverts": [
    {
      "target": "src/durability_sweep.py",
      "mutation": "unexpected gh call \u2192 DELIBERATELY-BROKEN",
      "test": "test_canned_gh_stub_rejects_unexpected_commands"
    },
    {
      "target": "src/feature_scan.py",
      "mutation": "RULE OF THREE \u2192 DELIBERATELY-BROKEN",
      "test": "test_feature_scan_apply_and_format_unlogged"
    },
    {
      "target": "src/strategy_experiment.py",
      "mutation": "invalid agents value \u2192 DELIBERATELY-BROKEN",
      "test": "test_strategy_coerce_and_normalize_arms_edges"
    }
  ]
}

## Focused baseline result

```json
{
  "command": "/opt/anaconda3/bin/python3 -m pytest -q --tb=short tests/test_issue843_ranked_coverage.py::test_canned_gh_stub_rejects_unexpected_commands tests/test_issue843_ranked_coverage.py::test_selftest_pops_absent_orch_state_dir tests/test_issue843_ranked_coverage.py::test_router_helpers_cover_edge_returns tests/test_issue843_ranked_coverage.py::test_router_heartbeat_and_exploration_short_circuits tests/test_issue843_ranked_coverage.py::test_router_pacing_choice_edge_branches tests/test_issue843_ranked_coverage.py::test_router_select_unknown_task_and_profile_miss tests/test_issue843_ranked_coverage.py::test_router_learned_ranks_and_plan_failures tests/test_issue843_ranked_coverage.py::test_router_main_cli_paths tests/test_issue843_ranked_coverage.py::test_router_selftest_restores_preset_exploration_env tests/test_issue843_ranked_coverage.py::test_feature_scan_heartbeat_errors_and_unparseable tests/test_issue843_ranked_coverage.py::test_feature_scan_apply_and_format_unlogged tests/test_issue843_ranked_coverage.py::test_effectiveness_lift_and_main_cli tests/test_issue843_ranked_coverage.py::test_effectiveness_closes_only_connections_it_opened tests/test_issue843_ranked_coverage.py::test_redirect_record_without_proposal_source_raises tests/test_issue843_ranked_coverage.py::test_redirect_bare_report_rejection_keeps_else_assert_unreachable tests/test_issue843_ranked_coverage.py::test_strategy_invalid_single_arm_raises_before_selftest_assert_false tests/test_issue843_ranked_coverage.py::test_strategy_coerce_and_normalize_arms_edges tests/test_issue843_ranked_coverage.py::test_strategy_shape_rate_and_open_pr_helpers tests/test_issue843_ranked_coverage.py::test_strategy_gh_subjects_success_and_pagination tests/test_issue843_ranked_coverage.py::test_strategy_select_subject_and_freeze tests/test_issue843_ranked_coverage.py::test_strategy_normalize_load_read_format_and_prepare tests/test_issue843_ranked_coverage.py::test_strategy_refresh_and_main_cli tests/test_issue843_ranked_coverage.py::test_strategy_remaining_ci_gaps tests/test_issue843_ranked_coverage.py::test_strategy_build_plan_from_args tests/test_range_lane_rollout_coverage_gaps.py::test_offline_selftest_restores_prior_handoff_and_clears_added_rate",
  "exit_code": 0,
  "seconds": 1.826,
  "failure_excerpt": "",
  "stdout_tail": ".........................                                                [100%]\n25 passed in 0.74s\n",
  "stderr_tail": ""
}
```

## Mutation mapping (one production break per added test)

Machine-readable: `docs/evidence/issue-843-acceptance/mutation-receipt.json`

| mutation_id | node_id(s) | target | break exit | restored pass |
|---|---|---|---:|---:|
| `durability-unexpected-gh` | `test_canned_gh_stub_rejects_unexpected_commands` | `src/durability_sweep.py` | 1 | 0 |
| `durability-pop-absent-state-dir` | `test_selftest_pops_absent_orch_state_dir` | `src/durability_sweep.py` | 1 | 0 |
| `router-drain-urgency-sign` | `test_router_helpers_cover_edge_returns` | `src/router.py` | 1 | 0 |
| `router-thompson-single-pool` | `test_router_heartbeat_and_exploration_short_circuits` | `src/router.py` | 1 | 0 |
| `router-pacing-nonpool-winner` | `test_router_pacing_choice_edge_branches` | `src/router.py` | 1 | 0 |
| `router-unknown-task-none` | `test_router_select_unknown_task_and_profile_miss` | `src/router.py` | 1 | 0 |
| `router-claim-race-reason` | `test_router_learned_ranks_and_plan_failures` | `src/router.py` | 1 | 0 |
| `router-main-dry-run-json` | `test_router_main_cli_paths` | `src/router.py` | 1 | 0 |
| `router-selftest-restore-exploration-rate` | `test_router_selftest_restores_preset_exploration_env` | `src/router.py` | 1 | 0 |
| `feature-scan-unparseable` | `test_feature_scan_heartbeat_errors_and_unparseable` | `src/feature_scan.py` | 1 | 0 |
| `feature-scan-rule-of-three` | `test_feature_scan_apply_and_format_unlogged` | `src/feature_scan.py` | 1 | 0 |
| `effectiveness-lift-verdict` | `test_effectiveness_lift_and_main_cli` | `src/capability_effectiveness.py` | 1 | 0 |
| `effectiveness-close-borrowed-conn` | `test_effectiveness_closes_only_connections_it_opened` | `src/capability_effectiveness.py` | 1 | 0 |
| `redirect-missing-proposal-guard` | `test_redirect_record_without_proposal_source_raises` | `src/redirect_shadow.py` | 1 | 0 |
| `redirect-missing-proposal-guard` | `test_redirect_bare_report_rejection_keeps_else_assert_unreachable` | `src/redirect_shadow.py` | 1 | 0 |
| `strategy-single-arm-guard` | `test_strategy_invalid_single_arm_raises_before_selftest_assert_false` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-invalid-agents` | `test_strategy_coerce_and_normalize_arms_edges` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-shape-rate-invalid-bad` | `test_strategy_shape_rate_and_open_pr_helpers` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-gh-pagination-unknown` | `test_strategy_gh_subjects_success_and_pagination` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-select-subject-shape` | `test_strategy_select_subject_and_freeze` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-hypothesis-not-found` | `test_strategy_normalize_load_read_format_and_prepare` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-refresh-requires-exp-id` | `test_strategy_refresh_and_main_cli` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-matches-shape-bug-label` | `test_strategy_remaining_ci_gaps` | `src/strategy_experiment.py` | 1 | 0 |
| `strategy-build-plan-hypothesis` | `test_strategy_build_plan_from_args` | `src/strategy_experiment.py` | 1 | 0 |
| `range-rollout-restore-handoff` | `test_offline_selftest_restores_prior_handoff_and_clears_added_rate` | `src/range_lane_rollout.py` | 1 | 0 |

## Coverage authority (seven ranked targets)

Ranking source: issue #843 / Orchestrator blended coverage initiative (escaped-defect priority, then churn, then uncovered mass).

| Target | CI missing before (#845 baseline run 39496947363) | After #845 (PR body, Gate run 39505581812 @ `2b96ffc`) | Exclusions |
|---|---:|---:|---|
| `range_lane_rollout` | 0 (prior commit) | 0 | — |
| `durability_sweep` | 3 | 0 | — |
| `router` | 54 | 0 | — |
| `feature_scan` | 13 | 0 | — |
| `capability_effectiveness` | 7 | 2 lines (910, 925 selftest-only closes) | unreachable while `_edge_rows` contract holds |
| `redirect_shadow` | 1 | 1 line (1383 selftest else) | unreachable while bare reports are rejected |
| `strategy_experiment` | 116 | 1 line (1037 selftest assert False) | unreachable while normalize_arm raises |

Local combined `verify.py --coverage` was not re-run in this repair (runtime budget); Gate coverage on the merge commit remains authoritative (`39512253651` published verify aggregate).

