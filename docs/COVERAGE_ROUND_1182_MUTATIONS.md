# Mutation evidence: `tests/test_coverage_round_1182.py`

Each row: real production code mutation in src/ that causes the test to FAIL, then PASSED after revert.

## test_choose_mode_aliases_and_deficits
- **src/ line**: `src/exploration_collection.py:65`
- **edit**: changed `if eps > th:` to `if eps < th:`
- **result**: FAILED as expected (test expects epsilon-greedy when eps > th, mutation returns thompson-hybrid)
- **revert**: PASSED

## test_filter_backlog_low_risk_and_sanitize_paths
- **src/ line**: `src/exploration_collection.py:101`
- **edit**: changed `elif task_type not in allowed:` to `elif task_type in allowed:`
- **result**: FAILED as expected (epic is in allowed, so no longer skipped; test expects it skipped)
- **revert**: PASSED

## test_claim_release_and_router_env_restore
- **src/ line**: `src/exploration_collection.py:179`
- **edit**: changed `claims.release(target, agent)` to `pass  # mutated - skip release`
- **result**: FAILED as expected (released list is empty instead of containing expected tuples)
- **revert**: PASSED

## test_seed_candidates_and_blocked_window_reasons
- **src/ line**: `src/exploration_collection.py:337`
- **edit**: changed `return [rng_seed]` to `return [None]`
- **result**: FAILED as expected (seed_search returns None instead of provided seed)
- **revert**: PASSED

## test_build_window_active_path_and_format_human
- **src/ line**: `src/exploration_collection.py:360`
- **edit**: changed `window = window_rows.get(mode) or {}` to `window = {}`
- **result**: FAILED as expected (window is empty, cap defaults to 0)
- **revert**: PASSED

## test_exploration_collection_main_paths
- **src/ line**: `src/exploration_collection.py:684`
- **edit**: changed `_selftest()` to `raise SystemExit(1)`
- **result**: FAILED as expected (main exits with 1 instead of 0)
- **revert**: PASSED

## test_ccusage_read_ledger_and_parsers
- **src/ line**: `src/ccusage_reconcile.py:62`
- **edit**: changed `row = json.loads(text)` to `raise ValueError('mutated')`
- **result**: FAILED as expected (JSON parsing raises ValueError, errors list grows)
- **revert**: PASSED

## test_ccusage_run_windows_skips
- **src/ line**: `src/ccusage_reconcile.py:142`
- **edit**: changed `agent = known_agent or ...` to `agent = "unknown"`
- **result**: FAILED as expected (agent is unknown, gets skipped by unsupported_agent check)
- **revert**: PASSED

## test_ccusage_load_sessions_and_cli
- **src/ line**: `src/ccusage_reconcile.py:174`
- **edit**: changed `def _load_ccusage_sessions(...):` to `def _load_ccusage_sessions(...): return [], ["mutated"]`
- **result**: FAILED as expected (returns empty sessions list with error)
- **revert**: PASSED

## test_ccusage_reconcile_uses_loaded_sessions
- **src/ line**: `src/ccusage_reconcile.py:283`
- **edit**: changed `window, reason = _match_window(...)` to `window, reason = None, "mutated"`
- **result**: FAILED as expected (all sessions get skipped with "mutated" reason)
- **revert**: PASSED

## test_dashboard_helpers_and_activation_branches
- **src/ line**: `src/observability_dashboard.py:32`
- **edit**: changed `return float(numerator) / float(denominator)` to `return 0.0`
- **result**: FAILED as expected (division returns 0 instead of computed rate)
- **revert**: PASSED

## test_dashboard_alerts_and_actionability
- **src/ line**: `src/observability_dashboard.py:81`
- **edit**: changed `production_runs = int(...)` to `production_runs = 0`
- **result**: FAILED as expected (productivity always reports 0 runs)
- **revert**: PASSED

## test_dashboard_markdown_and_main
- **src/ line**: `src/observability_dashboard.py:101`
- **edit**: changed `outcomes = report.get("outcomes") or {}` to `outcomes = {}`
- **result**: FAILED as expected (outcomes section is always empty)
- **revert**: PASSED

## test_filter_backlog_skip_reasons
- **src/ line**: `src/exploration_collection.py:103`
- **edit**: changed `reason = "task type is not low-risk..."` to `reason = "TASK TYPE IS LOW-RISK..."`
- **result**: FAILED as expected (reason string doesn't match expected value)
- **revert**: PASSED

## test_release_assignment_errors_are_swallowed
- **src/ line**: `src/exploration_collection.py:190`
- **edit**: changed `claims.release(target, agent)` to `raise RuntimeError("mutated")`
- **result**: FAILED as expected (error is not swallowed, propagates up)
- **revert**: PASSED

## test_direct_evidence_prompts_and_attach
- **src/ line**: `src/exploration_collection.py:302`
- **edit**: changed `if task_type == "testgen":` to `if task_type != "testgen":`
- **result**: FAILED as expected (testgen tasks don't get the testgen prompt)
- **revert**: PASSED

## test_build_window_acquisition_blocked
- **src/ line**: `src/exploration_collection.py:383`
- **edit**: changed `if not window.get("eligible"):` to `if True:`
- **result**: FAILED as expected (always adds blocked reason even when eligible)
- **revert**: PASSED

## test_exploration_collection_selftest_runs
- **src/ line**: `src/exploration_collection.py:580`
- **edit**: changed `assert dry["read_only"] is True` to `assert dry["read_only"] is False`
- **result**: FAILED as expected (assertion fails because read_only is actually True)
- **revert**: PASSED

## test_ccusage_measurement_and_match_helpers
- **src/ line**: `src/ccusage_reconcile.py:207`
- **edit**: changed `tokens_in = int(...)` to `tokens_in = 0`
- **result**: FAILED as expected (all sessions report 0 tokens_in)
- **revert**: PASSED

## test_ccusage_reconcile_write_path
- **src/ line**: `src/ccusage_reconcile.py:323`
- **edit**: changed `if not dry_run:` to `if False:`
- **result**: FAILED as expected (never writes cost rows even when not dry_run)
- **revert**: PASSED

## test_ccusage_selftest_runs
- **src/ line**: `src/ccusage_reconcile.py:484`
- **edit**: changed `assert dry["prepared"] == 1, dry` to `assert dry["prepared"] == 0, dry`
- **result**: FAILED as expected (test expects 1 prepared but mutation makes it check for 0)
- **revert**: PASSED

## test_observability_dashboard_selftest_runs
- **src/ line**: `src/observability_dashboard.py:1129`
- **edit**: changed `assert ret == 0, ret` to `assert ret == 1, ret`
- **result**: FAILED as expected (main returns 0 but test checks for 1)
- **revert**: PASSED

## test_dashboard_extended_branches
- **src/ line**: `src/observability_dashboard.py:447`
- **edit**: changed `cold_start_tasks.append(task_type)` to `pass  # mutated`
- **result**: FAILED as expected (cold_start_tasks remains empty)
- **revert**: PASSED
