# Issue #828 closer acceptance (repair of merged PR #833)

Acceptance report for the fifteen tests in `tests/test_issue828_coverage.py` and
`tests/test_issue828_round2_coverage.py`. Production code is unchanged; this document
records break-and-revert proof, separately labeled coverage evidence, and usage-guard isolation.

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

## Coverage evidence (six ranked targets; checked 2026-11-03)

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

**Saved main measurement (`verify.py --coverage`, 2026-11-03, reported exit 0, ~92m wall):**
pytest plus selftest subprocess coverage, with **statement-only counters**. This is a historical
measurement recorded by PR #1069, not a fresh run or an isolated measurement of PR #833's effect.

| Module | Saved combined-run missing statements |
|--------|--------------------------------------:|
| `src/keepalive_outcomes.py` | 45 |
| `src/issue_quality.py` | 5 |
| `src/experiment_recovery.py` | 9 |
| `src/capability_compiler.py` | 10 |
| `src/rail_exercise.py` | 20 |
| `src/research_usage_guard.py` | 27 |
| **Six-target total** | **116** |

Evidence: the existing local artifact
`/Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__828__opener/coverage.json`,
coverage.py `7.16.2`, timestamp `2026-11-03T03:37:08.681390`. Its metadata explicitly says
`branch_coverage: false`; neither file summaries nor totals contain branch counters. Repository-wide
totals are 122005 statements, 103064 covered lines, 18941 missing lines (84.5% statement coverage).
The artifact is local and is not added by this documentation-only repair; the relevant counters
are transcribed above for review.

**Exact achieved statements+branches delta: UNKNOWN. Exact shortfall against ~180 units: UNKNOWN.**
The issue-open ranking preserves only aggregate statements+branches misses, without a split into
statements and branches or the underlying historical branch report. The saved Gate excerpt preserves
pytest line misses only; the later combined-run JSON did not collect branch coverage. No matching
historical before/after statements+branches counters are available in these retained reports.
Missing branch evidence cannot be reconstructed from line misses. The later main snapshot also
includes intervening changes (for example, keepalive has 983 statements versus 978 in Gate #833),
so these snapshots do not isolate PR #833's contribution. No subtraction across these measurements
is valid, and no exact achieved-unit or remaining-unit claim is made.

**Partial delivery:** PR #833 delivered fifteen hermetic tests, with per-test mutation evidence
retained above. Those tests exercise selected recovery, rail validation, attribution/ingestion,
issue-quality and usage-guard behavior; they do not demonstrate full coverage of the ranked targets
or attainment of the ~180-unit goal. All six have statement gaps in the saved combined-run report,
and their residual branch gaps are unknown. No suite was rerun to manufacture a comparable metric.

**Unfinished target notes (functions and counts from the saved combined-run snapshot):**

- **`keepalive_outcomes.py` — 45 missing statements:** Tests inject PR, dispatch, policy and
  attribution evidence, and stub CLI ingestion. Residuals include default GitHub/backfill batching,
  registry and fetch error handling, existing-outcome skips, dry-run non-agent accounting, verifier
  recording and shadow-error paths. The injected paths do not establish these default adapters and
  failure cases; the saved report gives no residual branch count.
- **`issue_quality.py` — 5 missing statements:** Tests exercise parse failures, parallel-fetch
  errors and human output with `build_report` stubbed. Residuals are the default `_run_json`
  delegation, invalid linked-number handling, an empty parallel-fetch return and a skipped record.
  Selected malformed inputs do not exercise every fallback; branch completeness remains unknown.
- **`experiment_recovery.py` — 9 missing statements:** Tests inject GitHub spec fetching and
  recovery dependencies, and stub `plan` for CLI output. Seven residual statements are in `_spec_for`'s
  default subprocess fetch/JSON/error path; two are in `restore_experiment`'s exception handler.
  These adapters and failure handling are not established by the injected cases; the gap is not
  simply a CLI/apply tail, and no branch total was retained.
- **`capability_compiler.py` — 10 missing statements:** Existing hermetic coverage includes
  `tests/test_issue807_capability_compiler_*.py`. Saved residuals include a workflow-rail skip,
  missing skill-frontmatter name, package-hash and strict-schema failures, duplicate-role and
  selector validation, and role/playbook candidate returns and validation. These remain unexercised
  even in the saved combined run; equal numeric counts in differently measured reports do not
  establish an unchanged gap or that additional selftest instrumentation alone would close it.
- **`rail_exercise.py` — 20 missing statements:** Issue-828 tests cover `_break_ok` contract edges.
  Residuals include contract-load errors, command-expansion fallbacks, subprocess timeout handling,
  an expected-marker success, contract rejection, recording/advisor failures, a selftest fallback
  and CLI heartbeat errors. These coordinator, sandbox and error paths exceed the selected contract
  tests. The saved combined run already includes selftests; its statement count cannot be equated
  to the issue-open statements+branches count.
- **`research_usage_guard.py` — 27 missing statements:** The test uses an empty temporary ledger,
  injects `conn`, forces invalid configuration, and stubs CLI report functions. Residuals include
  schema setup, observation/read errors, numeric-limit validation, connection cleanup, budget/share
  alerts and blocking, malformed report JSON, `BUDGET_BLOCKED` health and default output-path
  selection. Empty-ledger and stubbed report cases do not establish those populated-ledger and
  default-resource paths; residual branches were not measured.

## Documentation repair verification

Outcome 1: separately labeled evidence, with exact delta and shortfall UNKNOWN and limitations
explained for all six targets. Check date: 2026-11-03. Reuse the saved report; do not rerun the suite.

```sh
/opt/anaconda3/bin/python3 -m json.tool /Users/teacher/.codex/orchestrator/worktrees/stranske__Orchestrator__828__opener/coverage.json > /private/tmp/issue828-coverage.pretty.json
git diff --check
```

Both commands exit 0 with no stdout. Final document inspection confirms there is no mixed-metric
subtraction or exact delta/shortfall claim. No production code or tests changed.
