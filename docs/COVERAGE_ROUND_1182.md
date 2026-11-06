# Combined Gate coverage — round #1182

Targets: `exploration_collection.py`, `observability_dashboard.py`, `ccusage_reconcile.py`.

Measurements: `/opt/anaconda3/bin/python3`, `coverage` JSON from `pytest --cov=… --cov-branch` (2026-11-06). **Baseline** for `exploration_collection` + `observability_dashboard` is `tests/test_stage2_needs_recording.py`, `tests/test_observability_activation.py`, `tests/test_model_profile_trial.py`, and `tests/test_exploration_window_extension.py` only (same modules, before `test_coverage_round_1182.py`). **Baseline for `ccusage_reconcile`**: that suite never imports the module, so “before” is treated as **uncovered = full statement and branch counts** (no lines executed). **After** is `tests/test_coverage_round_1182.py` alone for all three modules.

Deliberate break / restore evidence for each test in this module: `docs/COVERAGE_ROUND_1182_MUTATIONS.md`.

## Per-target uncovered statements / uncovered branches

| Module | Before (stmt / branch) | After (stmt / branch) | Δ stmt | Δ branch |
|--------|------------------------|------------------------|--------|----------|
| exploration_collection.py | 272 / 108 | 0 / 3 | −272 | −105 |
| observability_dashboard.py | 331 / 145 | 7 / 17 | −324 | −128 |
| ccusage_reconcile.py | 285 / 72 | 7 / 2 | −278 | −70 |

(`coverage` “missing_lines” / “missing_branches” in combined JSON reports.)

## Combined totals (three targets)

| | Before | After | Reduction |
|---|--------|-------|-----------|
| Uncovered statements | 888 | 14 | **874** |
| Uncovered branches | 325 | 22 | **303** |

## Shortfall vs ~180 uncovered statements (combined)

After this round the combined **residual** uncovered statement count is **14**, which is **below** the informal ~180 ceiling — **no shortfall** against that bar. Residual misses are mostly narrow CLI/selftest branches (`ccusage_reconcile` subprocess edge lines; `observability_dashboard` report-builder branches; three partial branches in `exploration_collection` release paths).

## Note on `src/verify.py --coverage`

A full-repo `python3 src/verify.py --coverage` was **not** awaited to completion here (prior run exceeded a 10-minute tool window and was stopped). The table above is the **direct pytest + coverage** measurement for the three modules driven by `tests/test_coverage_round_1182.py`; CI’s combined Gate run remains authoritative for repo-wide coverage.
