# Issue #828 closer acceptance (repair of merged PR #833)

Acceptance report for the fifteen tests in `tests/test_issue828_coverage.py` and
`tests/test_issue828_round2_coverage.py`. Production code is unchanged; this document
records break-and-revert proof, combined-coverage accounting, and usage-guard isolation.

## Per-test break-and-revert

Command for each row: `/opt/anaconda3/bin/python3 -m pytest --no-cov -q <node>` after a
single production mutation, then byte-identical restore and re-run (PASS).

| # | pytest node | Named mutation | Break (exit 1) | Restore |
|---|-------------|----------------|----------------|---------|
| 1 | `tests/test_issue828_coverage.py::test_arm_diff_uses_merge_base_and_refuses_missing_branch` | `experiment_recovery.arm_diff` returns `None` instead of git diff | `AssertionError` on diff equality | PASS |
| 2 | `tests/test_issue828_coverage.py::test_restore_partial_experiment_preserves_missing_arm_and_stub_provenance` | `restore_experiment` status never `partial` | `assert result == {...}` status mismatch | PASS |
| 3 | `tests/test_issue828_coverage.py::test_recovery_plan_applies_limit_and_reports_subject_correlation` | `plan()` ignores `limit` slice | `assert restored == ["tick-1-owner-repo-1"]` | PASS |
| 4 | `tests/test_issue828_coverage.py::test_rail_break_result_requires_a_failure_or_explicit_check` | `rail_exercise._break_ok` accepts all-zero break runs | `(passed, reason) == (False, ...)` fails | PASS |
| 5 | `tests/test_issue828_coverage.py::test_recovery_does_not_launch_processes_when_all_dependencies_are_injected` | `restore_experiment` sets `status` broken when `apply=False` | `assert result["status"] == "recoverable"` | PASS |
| 6 | `tests/test_issue828_round2_coverage.py::test_work_type_and_label_author_helpers_cover_each_class` | `keepalive_outcomes._work_type` renovate branch returns `issue` | renovate/tooling/docs classifiers fail | PASS |
| 7 | `tests/test_issue828_round2_coverage.py::test_process_ignore_and_attach_commit_identities` | `_maybe_mark_process_ignore` returns `oc` without note | `assert tagged["notes"] == "process_ignore=duplicate"` | PASS |
| 8 | `tests/test_issue828_round2_coverage.py::test_export_policy_dispatch_metadata_and_fetch_times` | `_export_policy_at` validation disabled (`if False`) | `assert good == {...}` / `assert ko._export_policy_at(1) is None` | PASS |
| 9 | `tests/test_issue828_round2_coverage.py::test_backfill_evidence_batch_and_attribution_apply` | `_update_attribution` writes agent `cursor` | `assert agent == "codex"` | PASS |
| 10 | `tests/test_issue828_round2_coverage.py::test_local_dispatch_bot_human_attribution_and_stamp_policy` | `_stamp_existing_dispatch_policy` sets `policy_settled_ts` to `0` | `assert meta["policy_settled_ts"] == 99` | PASS |
| 11 | `tests/test_issue828_round2_coverage.py::test_outcome_for_pr_and_ingest_agent_and_non_agent_paths` | `ingest_keepalive_outcomes` zeroes `runs_recorded` before return | `assert summary["runs_recorded"] >= 2` | PASS |
| 12 | `tests/test_issue828_round2_coverage.py::test_keepalive_main_backfill_and_ingest_cli` | `main()` `--backfill-attribution` returns exit `1` | `assert ko.main([...]) == 0` | PASS |
| 13 | `tests/test_issue828_round2_coverage.py::test_issue_quality_parse_edges_parallel_fetch_and_human_main` | `issue_quality.format_human` underpowered-none line altered | substring assertion on human report | PASS |
| 14 | `tests/test_issue828_round2_coverage.py::test_experiment_recovery_git_spec_unevaluated_and_main` | `experiment_recovery._canonical_repo` returns raw target | `assert ... == "owner/repo"` | PASS |
| 15 | `tests/test_issue828_round2_coverage.py::test_research_usage_guard_report_main_and_config_alert` | `assess_and_record_opportunity` reason `guard_config_broken` | `assert result["reason"] == "guard_config_invalid"` | PASS |

Full module restore run:

```
/opt/anaconda3/bin/python3 -m pytest --no-cov -q tests/test_issue828_coverage.py tests/test_issue828_round2_coverage.py
...............                                                          [100%]
15 passed in 1.01s
```

## Usage-guard test isolation (`test_research_usage_guard_report_main_and_config_alert`)

- **Runtime dirs:** module autouse `isolated_runtime` sets `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`,
  `ORCH_FEEDBACK_DB`, and `ORCH_CAPABILITIES_PATH` to `tmp_path`; `feedback.DB_PATH` is patched to
  the same temp brain path.
- **Process/network guard:** autouse fixture replaces `subprocess.run|Popen|call|check_*` and
  `socket.socket.connect|connect_ex|sendto` with a forbidden stub; post-test `assert attempted == []`.
- **Database:** `sqlite3.connect(tmp_path / "rug.db")` with `conn=` passed into
  `write_usage_report`, `assess_and_record_opportunity`; no live `$ORCH_FEEDBACK_DB` reads required
  for the empty-ledger observability path.
- **Side effects stubbed:** `rug.capabilities.production_heartbeat` raises `RuntimeError("ignore")`;
  CLI segments monkeypatch `generate_usage_report` / `write_usage_report` so `main` does not touch disk
  beyond the explicit `report_path` / `out_path` under `tmp_path`.
- **Assertions exercised:** `health_status == "OBSERVABILITY_UNAVAILABLE"` with on-disk JSON equality;
  invalid `ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT` → `decision == "blocked_by_anomaly"` and
  `reason == "guard_config_invalid"`; `main report --fail-on-alert` exit `2`; JSON and write-report
  paths return `0` with expected `health_status`.

## Combined coverage (six ranked targets)

**Issue #828 ranked baseline (Gate combined ranking at issue open):** uncovered statements+branches
per target — `keepalive_outcomes` 74, `issue_quality` 25, `experiment_recovery` 29,
`capability_compiler` 10, `rail_exercise` 20, `research_usage_guard` 49 (**207 total**).

**PR #833 Gate result (pytest CI job, run `39459066137`, head at merge):** authoritative CI pass;
pytest-only missing **lines** on the six modules (not branches, not selftest subprocesses):

| Module | Missing lines (pytest CI @ #833) |
|--------|----------------------------------|
| `src/keepalive_outcomes.py` | 258 |
| `src/issue_quality.py` | 52 |
| `src/experiment_recovery.py` | 76 |
| `src/capability_compiler.py` | 10 |
| `src/rail_exercise.py` | 89 |
| `src/research_usage_guard.py` | 76 |

**Partial delivery vs ~180 combined units:** PR #833 added fifteen hermetic tests but did not fully
close ranked combined gaps on targets 1–3 and 6; targets 4–5 were advanced earlier on that PR branch.
Remaining mass on `capability_compiler` and `rail_exercise` is largely `--selftest`, CLI, and
live-shadow paths that pytest-only tables under-count; combined `verify.py --coverage` is required
for honest statement+branch totals (see PR body for current-main measurement output).

**Unfinished target notes:**

- **`capability_compiler.py`:** Ten ranked units at issue open; still ten missing lines in Gate #833
  pytest report. Hermetic tests live in `tests/test_issue807_capability_compiler_*.py`; residual
  units need combined selftest/CLI instrumentation, not more issue-828 module tests.
- **`rail_exercise.py`:** Twenty ranked units at issue open; eighty-nine missing pytest lines post-#833
  because coordinator exercise and sandbox subprocess paths dominate; issue-828 tests only cover
  `_break_ok` contract edges in `tests/test_issue828_coverage.py`.

**Current main combined (`verify.py --coverage`, 2026-11-03, exit 0, ~92m wall):** missing
statements per target from merged `coverage.json` (this run’s JSON has no branch counters):

| Module | Issue ranked s+b (before) | Combined missing stmts (after) | Δ |
|--------|-------------------------:|-------------------------------:|---:|
| `src/keepalive_outcomes.py` | 74 | 45 | 29 |
| `src/issue_quality.py` | 25 | 5 | 20 |
| `src/experiment_recovery.py` | 29 | 9 | 20 |
| `src/capability_compiler.py` | 10 | 10 | 0 |
| `src/rail_exercise.py` | 20 | 20 | 0 |
| `src/research_usage_guard.py` | 49 | 27 | 22 |
| **Six-target total** | **207** | **116** | **91** |

**max(0, 180 − 91) = 89** units still short of the ~180 combined-unit campaign delta
(using statement misses as the measurable combined proxy here; ranked baseline counted s+b).
