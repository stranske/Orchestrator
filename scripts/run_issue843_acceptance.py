#!/usr/bin/env python3
"""Scoped acceptance runner for stranske/Orchestrator#843 (post-#845 closer repair).

Runs the focused pytest baseline, applies one production mutation per added test (or a
shared mutation when every mapped test fails), records failure/restoration receipts, and
writes durable evidence under docs/evidence/issue-843-acceptance/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "issue-843-acceptance"
RECEIPT_JSON = EVIDENCE_DIR / "mutation-receipt.json"
REPORT_MD = EVIDENCE_DIR / "README.md"
PYTEST_TIMEOUT_SECONDS = 90

FOCUSED_TESTS = (
    "tests/test_issue843_ranked_coverage.py",
    "tests/test_range_lane_rollout_coverage_gaps.py",
)

ISSUE843_ADDED_NODE_IDS = (
    "tests/test_issue843_ranked_coverage.py::test_canned_gh_stub_rejects_unexpected_commands",
    "tests/test_issue843_ranked_coverage.py::test_selftest_pops_absent_orch_state_dir",
    "tests/test_issue843_ranked_coverage.py::test_router_helpers_cover_edge_returns",
    "tests/test_issue843_ranked_coverage.py::test_router_heartbeat_and_exploration_short_circuits",
    "tests/test_issue843_ranked_coverage.py::test_router_pacing_choice_edge_branches",
    "tests/test_issue843_ranked_coverage.py::test_router_select_unknown_task_and_profile_miss",
    "tests/test_issue843_ranked_coverage.py::test_router_learned_ranks_and_plan_failures",
    "tests/test_issue843_ranked_coverage.py::test_router_main_cli_paths",
    "tests/test_issue843_ranked_coverage.py::test_router_selftest_restores_preset_exploration_env",
    "tests/test_issue843_ranked_coverage.py::test_feature_scan_heartbeat_errors_and_unparseable",
    "tests/test_issue843_ranked_coverage.py::test_feature_scan_apply_and_format_unlogged",
    "tests/test_issue843_ranked_coverage.py::test_effectiveness_lift_and_main_cli",
    "tests/test_issue843_ranked_coverage.py::test_effectiveness_closes_only_connections_it_opened",
    "tests/test_issue843_ranked_coverage.py::test_redirect_record_without_proposal_source_raises",
    "tests/test_issue843_ranked_coverage.py::test_redirect_bare_report_rejection_keeps_else_assert_unreachable",
    "tests/test_issue843_ranked_coverage.py::test_strategy_invalid_single_arm_raises_before_selftest_assert_false",
    "tests/test_issue843_ranked_coverage.py::test_strategy_coerce_and_normalize_arms_edges",
    "tests/test_issue843_ranked_coverage.py::test_strategy_shape_rate_and_open_pr_helpers",
    "tests/test_issue843_ranked_coverage.py::test_strategy_gh_subjects_success_and_pagination",
    "tests/test_issue843_ranked_coverage.py::test_strategy_select_subject_and_freeze",
    "tests/test_issue843_ranked_coverage.py::test_strategy_normalize_load_read_format_and_prepare",
    "tests/test_issue843_ranked_coverage.py::test_strategy_refresh_and_main_cli",
    "tests/test_issue843_ranked_coverage.py::test_strategy_remaining_ci_gaps",
    "tests/test_issue843_ranked_coverage.py::test_strategy_build_plan_from_args",
    "tests/test_range_lane_rollout_coverage_gaps.py::test_offline_selftest_restores_prior_handoff_and_clears_added_rate",
)

PR845_RECEIPTS = {
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
            "mutation": "unexpected gh call → DELIBERATELY-BROKEN",
            "test": "test_canned_gh_stub_rejects_unexpected_commands",
        },
        {
            "target": "src/feature_scan.py",
            "mutation": "RULE OF THREE → DELIBERATELY-BROKEN",
            "test": "test_feature_scan_apply_and_format_unlogged",
        },
        {
            "target": "src/strategy_experiment.py",
            "mutation": "invalid agents value → DELIBERATELY-BROKEN",
            "test": "test_strategy_coerce_and_normalize_arms_edges",
        },
    ],
}


@dataclass(frozen=True)
class MutationSpec:
    mutation_id: str
    node_ids: tuple[str, ...]
    rel_path: str
    old: str
    new: str
    replace: str = "first"  # first | all
    description: str = ""


MUTATIONS: tuple[MutationSpec, ...] = (
    MutationSpec(
        "durability-unexpected-gh",
        (ISSUE843_ADDED_NODE_IDS[0],),
        "src/durability_sweep.py",
        'raise AssertionError(f"unexpected gh call: {args}")',
        'raise AssertionError(f"DELIBERATELY-BROKEN: {args}")',
        "all",
        "Canned-gh stub must fail on unexpected argv.",
    ),
    MutationSpec(
        "durability-pop-absent-state-dir",
        (ISSUE843_ADDED_NODE_IDS[1],),
        "src/durability_sweep.py",
        '            os.environ.pop("ORCH_STATE_DIR", None)',
        "            pass  # issue843-break: skip pop when ORCH_STATE_DIR was absent",
        "first",
        "_selftest must pop ORCH_STATE_DIR when it was unset on entry.",
    ),
    MutationSpec(
        "router-drain-urgency-sign",
        (ISSUE843_ADDED_NODE_IDS[2],),
        "src/router.py",
        "    return -round(unused_fraction * time_pressure, 4)",
        "    return round(unused_fraction * time_pressure, 4)",
        "first",
        "_drain_urgency successful path must stay negative.",
    ),
    MutationSpec(
        "router-thompson-single-pool",
        (ISSUE843_ADDED_NODE_IDS[3],),
        "src/router.py",
        "    if len(scored) < 2:\n        return None",
        "    if len(scored) < 2:\n        return scored[0]",
        "first",
        "Single-candidate exploration pool must return None.",
    ),
    MutationSpec(
        "router-pacing-nonpool-winner",
        (ISSUE843_ADDED_NODE_IDS[4],),
        "src/router.py",
        "        return scored[0], None",
        "        return scored[-1], None",
        "first",
        "Winner outside pacing pools must keep scored[0].",
    ),
    MutationSpec(
        "router-unknown-task-none",
        (ISSUE843_ADDED_NODE_IDS[5],),
        "src/router.py",
        "    if not spec:\n        return None",
        '    if not spec:\n        return {"agent": "codex", "state": "ok"}',
        "first",
        "Unknown task types must not select an agent.",
    ),
    MutationSpec(
        "router-claim-race-reason",
        (ISSUE843_ADDED_NODE_IDS[6],),
        "src/router.py",
        '"reason": "claim_race_lost",',
        '"reason": "claim_race_won",',
        "first",
        "Lost claim races must surface claim_race_lost rejections.",
    ),
    MutationSpec(
        "router-main-dry-run-json",
        (ISSUE843_ADDED_NODE_IDS[7],),
        "src/router.py",
        "        print(json.dumps(decision, indent=2))",
        '        print(json.dumps({"broken": True}))',
        "first",
        "CLI dry-run must emit the real routing decision JSON.",
    ),
    MutationSpec(
        "router-selftest-restore-exploration-rate",
        (ISSUE843_ADDED_NODE_IDS[8],),
        "src/router.py",
        '            os.environ["ORCH_EXPLORATION_RATE"] = old_exploration_rate',
        '            os.environ.pop("ORCH_EXPLORATION_RATE", None)',
        "first",
        "Preset ORCH_EXPLORATION_* must be restored after _selftest.",
    ),
    MutationSpec(
        "feature-scan-unparseable",
        (ISSUE843_ADDED_NODE_IDS[9],),
        "src/feature_scan.py",
        '        return False, f"unparseable ({type(exc).__name__})"',
        '        return True, "issue843-break"',
        "first",
        "Syntax errors must not look reusable.",
    ),
    MutationSpec(
        "feature-scan-rule-of-three",
        (ISSUE843_ADDED_NODE_IDS[10],),
        "src/feature_scan.py",
        "RULE OF THREE",
        "DELIBERATELY-BROKEN",
        "all",
        "Scan report must name RULE OF THREE for unlogged structures.",
    ),
    MutationSpec(
        "effectiveness-lift-verdict",
        (ISSUE843_ADDED_NODE_IDS[11],),
        "src/capability_effectiveness.py",
        '"helps" if entry["lift"] > 0 else "hurts" if entry["lift"] < 0 else "neutral"',
        '"hurts" if entry["lift"] > 0 else "helps" if entry["lift"] < 0 else "neutral"',
        "first",
        "Positive lift on measured arms must read helps.",
    ),
    MutationSpec(
        "effectiveness-close-borrowed-conn",
        (ISSUE843_ADDED_NODE_IDS[12],),
        "src/capability_effectiveness.py",
        "    close = conn is None",
        "    close = True",
        "first",
        "Caller-supplied connections must not be closed by _edge_rows.",
    ),
    MutationSpec(
        "redirect-missing-proposal-guard",
        ISSUE843_ADDED_NODE_IDS[13:15],
        "src/redirect_shadow.py",
        "    if not dispatch and proposal_json is None:\n        raise ValueError(",
        "    if False and not dispatch and proposal_json is None:\n        raise ValueError(",
        "first",
        "Bare redirect reports must be rejected before corpus write.",
    ),
    MutationSpec(
        "strategy-single-arm-guard",
        (
            ISSUE843_ADDED_NODE_IDS[15],
            ISSUE843_ADDED_NODE_IDS[22],
        ),
        "src/strategy_experiment.py",
        '        raise ValueError(f"single strategy must name exactly one agent: {agents!r}")',
        "        pass  # issue843-break",
        "first",
        "Invalid single-arm shapes must raise before selftest assert False.",
    ),
    MutationSpec(
        "strategy-invalid-agents",
        (ISSUE843_ADDED_NODE_IDS[16],),
        "src/strategy_experiment.py",
        '        raise ValueError(f"invalid agents value: {raw!r}")',
        '        raise ValueError(f"DELIBERATELY-BROKEN: {raw!r}")',
        "first",
        "_coerce_agents must reject non-list/non-str values.",
    ),
    MutationSpec(
        "strategy-shape-rate-invalid-bad",
        (ISSUE843_ADDED_NODE_IDS[17],),
        "src/strategy_experiment.py",
        '            raise RuntimeError("UNKNOWN: invalid broke-later rate")',
        "            pass  # issue843-break",
        "first",
        "_shape_rate must validate broke_later_rate bounds.",
    ),
    MutationSpec(
        "strategy-gh-pagination-unknown",
        (ISSUE843_ADDED_NODE_IDS[18],),
        "src/strategy_experiment.py",
        '            if open_prs["pageInfo"]["hasNextPage"]:\n                raise RuntimeError("open PR population pagination incomplete")',
        '            if False and open_prs["pageInfo"]["hasNextPage"]:\n                raise RuntimeError("open PR population pagination incomplete")',
        "first",
        "Incomplete GitHub pagination must surface UNKNOWN.",
    ),
    MutationSpec(
        "strategy-select-subject-shape",
        (ISSUE843_ADDED_NODE_IDS[19],),
        "src/strategy_experiment.py",
        '        raise RuntimeError("UNKNOWN: no recurring shape with sufficient broke-later evidence")',
        '        raise RuntimeError("issue843-break")',
        "first",
        "select_subject must require a recurring fleet shape.",
    ),
    MutationSpec(
        "strategy-hypothesis-not-found",
        (ISSUE843_ADDED_NODE_IDS[20],),
        "src/strategy_experiment.py",
        '    raise ValueError(f"hypothesis {hypothesis_id!r} not found")',
        "    pass  # issue843-break",
        "first",
        "load_hypothesis_arms must locate the requested hypothesis id.",
    ),
    MutationSpec(
        "strategy-refresh-requires-exp-id",
        (ISSUE843_ADDED_NODE_IDS[21],),
        "src/strategy_experiment.py",
        """        if not args.exp_id or args.prepare or args.subject:
            print(
                "--refresh-result requires --exp-id and cannot prepare a subject", file=sys.stderr
            )
            return 2""",
        """        if not args.exp_id or args.prepare or args.subject:
            print(
                "--refresh-result requires --exp-id and cannot prepare a subject", file=sys.stderr
            )
            return 0""",
        "first",
        "--refresh-result without --exp-id must exit 2 with guidance.",
    ),
    MutationSpec(
        "strategy-matches-shape-bug-label",
        (ISSUE843_ADDED_NODE_IDS[22],),
        "src/strategy_experiment.py",
        '        and ("bug" in labels or any(cue in title for cue in ("fix", "bug", "failure")))',
        '        and False and ("bug" in labels or any(cue in title for cue in ("fix", "bug", "failure")))',
        "first",
        "Bug label must infer fix-shaped subjects.",
    ),
    MutationSpec(
        "strategy-build-plan-hypothesis",
        (ISSUE843_ADDED_NODE_IDS[23],),
        "src/strategy_experiment.py",
        '        hypothesis = str(hyp.get("id"))',
        '        hypothesis = "issue843-break"',
        "first",
        "_build_plan_from_args must preserve hypothesis id from file.",
    ),
    MutationSpec(
        "range-rollout-restore-handoff",
        (ISSUE843_ADDED_NODE_IDS[24],),
        "src/range_lane_rollout.py",
        '        os.environ["HANDOFF_DIR"] = old_handoff',
        '        os.environ.pop("HANDOFF_DIR", None)',
        "first",
        "_selftest must restore prior HANDOFF_DIR when it was preset.",
    ),
)


def resolve_python(explicit: str | None) -> str:
    selected = explicit or os.environ.get("ORCH_STANDARD_PYTHON")
    if not selected and os.environ.get("VIRTUAL_ENV"):
        selected = str(Path(os.environ["VIRTUAL_ENV"]) / "bin" / "python")
    selected = selected or "/opt/anaconda3/bin/python3"
    path = Path(selected)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SystemExit(f"selected Python interpreter unavailable: {selected}")
    return str(path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_mutation(path: Path, spec: MutationSpec) -> tuple[bytes, str]:
    original = path.read_bytes()
    original_text = original.decode("utf-8")
    if spec.old not in original_text:
        raise ValueError(f"{spec.mutation_id}: needle missing in {path}")
    if spec.replace == "all":
        mutated = original_text.replace(spec.old, spec.new)
    else:
        mutated = original_text.replace(spec.old, spec.new, 1)
    try:
        path.write_bytes(mutated.encode("utf-8"))
    except BaseException:
        restore_file(path, original, sha256_bytes(original))
        raise
    return original, sha256_bytes(original)


def restore_file(path: Path, original: bytes, expected_sha: str) -> None:
    if sha256_bytes(original) != expected_sha:
        raise RuntimeError(f"original bytes sha256 mismatch for {path}")
    path.write_bytes(original)
    if sha256_bytes(path.read_bytes()) != expected_sha:
        raise RuntimeError(f"restore failed for {path}")
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        stem = path.stem
        for artifact in cache.glob(f"{stem}*.pyc"):
            artifact.unlink(missing_ok=True)


def run_pytest(
    python: str, node_ids: tuple[str, ...], *, extra_env: dict[str, str] | None = None
) -> dict:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)
    cmd = [python, "-m", "pytest", "-q", "--tb=short", "--color=no", *node_ids]
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="issue843-pytest-") as runtime:
        # Override even caller-provided values before pytest imports any modules.
        env["ORCH_STATE_DIR"] = str(Path(runtime) / "state")
        env["ORCH_LOCAL_RUNTIME"] = str(Path(runtime) / "runtime")
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    combined = (proc.stdout or "") + (proc.stderr or "")
    output_lines = combined.splitlines()
    failed_nodes = [line.split()[1] for line in output_lines if line.startswith("FAILED ")]
    has_errors = any(line.startswith("ERROR ") for line in output_lines)
    mapped_failure = (
        proc.returncode == 1 and not has_errors and all(node in failed_nodes for node in node_ids)
    )
    failure_line = next((line for line in output_lines if line.startswith("FAILED ")), "")
    if not failure_line and proc.returncode != 0:
        failure_line = combined.strip().splitlines()[-1] if combined.strip() else "nonzero exit"
    return {
        "command": " ".join(cmd),
        "exit_code": proc.returncode,
        "failed": mapped_failure,
        "failed_node_ids": failed_nodes,
        "seconds": round(time.perf_counter() - started, 3),
        "failure_excerpt": failure_line[:500],
        "stdout_tail": (proc.stdout or "")[-1500:],
        "stderr_tail": (proc.stderr or "")[-1500:],
    }


def run_focused_baseline(python: str) -> dict:
    return run_pytest(python, ISSUE843_ADDED_NODE_IDS)


def run_mutations(python: str, *, limit: int | None = None) -> list[dict]:
    receipts: list[dict] = []
    specs = MUTATIONS if limit is None else MUTATIONS[:limit]
    for spec in specs:
        path = REPO_ROOT / spec.rel_path
        original, original_sha = apply_mutation(path, spec)
        try:
            break_runs = []
            for node_id in spec.node_ids:
                break_runs.append({"node_id": node_id, **run_pytest(python, (node_id,))})
            for run in break_runs:
                if run["exit_code"] != 1 or not run["failed"]:
                    raise SystemExit(
                        f"{spec.mutation_id} did not fail {run['node_id']} as a mapped test failure "
                        f"(exit {run['exit_code']})"
                    )
            restore_file(path, original, original_sha)
            restore_runs = []
            for node_id in spec.node_ids:
                restore_runs.append({"node_id": node_id, **run_pytest(python, (node_id,))})
            for run in restore_runs:
                if run["exit_code"] != 0:
                    raise SystemExit(
                        f"{spec.mutation_id} did not pass after restore for {run['node_id']}"
                    )
            entry = {
                "mutation_id": spec.mutation_id,
                "node_ids": list(spec.node_ids),
                "target": spec.rel_path,
                "description": spec.description,
                "original_sha256": original_sha,
                "restored_sha256": sha256_bytes(path.read_bytes()),
                "break": break_runs,
                "restored_pass": restore_runs,
                "verdict": "PASS",
            }
            receipts.append(entry)
        except BaseException:
            restore_file(path, original, original_sha)
            raise
    return receipts


def write_report(
    *,
    python: str,
    baseline: dict,
    receipts: list[dict],
    git_head: str,
) -> None:
    # Validate the complete proof before touching either published evidence file.
    expected = {spec.mutation_id: spec for spec in MUTATIONS}
    mapped_nodes = {node for spec in MUTATIONS for node in spec.node_ids}
    if mapped_nodes != set(ISSUE843_ADDED_NODE_IDS) or baseline["exit_code"] != 0:
        raise ValueError("incomplete node mapping or failing baseline")
    seen = set()
    for receipt in receipts:
        mutation_id = receipt["mutation_id"]
        spec = expected.get(mutation_id)
        if (
            mutation_id in seen
            or spec is None
            or (tuple(receipt["node_ids"]) != spec.node_ids or receipt["target"] != spec.rel_path)
        ):
            raise ValueError("incomplete or duplicate mutation mapping")
        seen.add(mutation_id)
        if receipt["verdict"] != "PASS" or (
            receipt["original_sha256"] != receipt["restored_sha256"]
        ):
            raise ValueError("invalid restoration receipt")
        for node in receipt["node_ids"]:
            broken = [run for run in receipt["break"] if run["node_id"] == node]
            restored = [run for run in receipt["restored_pass"] if run["node_id"] == node]
            if (
                len(broken) != 1
                or len(restored) != 1
                or not (
                    broken[0]["exit_code"] == 1
                    and broken[0]["failed"]
                    and node in broken[0]["failed_node_ids"]
                    and restored[0]["exit_code"] == 0
                )
            ):
                raise ValueError("missing mapped failure or restored pass")
    if seen != set(expected):
        raise ValueError("incomplete mutation mapping")
    pr845 = PR845_RECEIPTS
    lines = [
        "# Issue #843 acceptance evidence (closer repair)",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Git HEAD: `{git_head}`",
        f"Python: `{python}`",
        "",
        "## Reproducible commands",
        "",
        "Focused baseline (25 added tests):",
        "",
        "```bash",
        f"{python} scripts/run_issue843_acceptance.py --baseline-only",
        "```",
        "",
        "Full acceptance runner (baseline + deliberate mutations + receipts):",
        "",
        "```bash",
        f"{python} scripts/run_issue843_acceptance.py --write-report",
        "```",
        "",
        "Combined coverage (CI only; do not run the full pipeline locally):",
        "",
        "```bash",
        'ORCH_STATE_DIR="$(mktemp -d)" ORCH_LOCAL_RUNTIME="$(mktemp -d)" \\',
        "  python3 src/verify.py --coverage",
        "```",
        "",
        "CI uses sharded `python3 src/verify.py --pytest-shard i/6 --coverage` then",
        "`python3 src/verify.py --aggregate shard-evidence --coverage --floor-may-lag` on pull requests",
        "(see `.github/workflows/ci.yml`). Branch coverage is included in Gate combined metrics",
        "(`branch=false` in pytest-cov omits branches; this repository's Gate uses combined statement+branch).",
        "",
        "## PR #845 recovered receipts (not absent when comments are unreachable)",
        "",
        json.dumps(pr845, indent=2),
        "",
        "## Focused baseline result",
        "",
        "```json",
        json.dumps(baseline, indent=2),
        "```",
        "",
        "## Mutation mapping (every mapped break, including shared tests)",
        "",
        f"Machine-readable: `{RECEIPT_JSON.relative_to(REPO_ROOT)}`",
        "",
        "| mutation_id | node_id(s) | target | break exit | restored pass |",
        "|---|---|---|---:|---:|",
    ]
    for receipt in receipts:
        for node_id in receipt["node_ids"]:
            break_exit = next(r["exit_code"] for r in receipt["break"] if r["node_id"] == node_id)
            restore_exit = next(
                r["exit_code"] for r in receipt["restored_pass"] if r["node_id"] == node_id
            )
            lines.append(
                f"| `{receipt['mutation_id']}` | `{node_id.split('::')[-1]}` | "
                f"`{receipt['target']}` | {break_exit} | {restore_exit} |"
            )
    lines.extend(
        [
            "",
            "## Coverage limits (seven ranked targets)",
            "",
            "Ranking source: issue #843 / Orchestrator blended coverage initiative "
            "(escaped-defect priority, then churn, then uncovered mass).",
            "",
            "| Target | Issue #843 baseline units (statements + branches) | Local after-#845 missing-line estimate | Exclusions |",
            "|---|---:|---:|---|",
            "| `range_lane_rollout` | 2 | 0 | — |",
            "| `durability_sweep` | 3 | 0 | — |",
            "| `router` | 54 | 0 | — |",
            "| `feature_scan` | 13 | 0 | — |",
            "| `capability_effectiveness` | 7 | 2 lines (910, 925 selftest-only closes) | unreachable while `_edge_rows` contract holds |",
            "| `redirect_shadow` | 1 | 1 line (1383 selftest else) | unreachable while bare reports are rejected |",
            "| `strategy_experiment` | 116 | 1 line (1037 selftest assert False) | unreachable while normalize_arm raises |",
            "",
            "The after-values are local pytest missing-line estimates recovered from #845, "
            "not Gate combined coverage. They omit branch accounting and are not comparable "
            "to the issue's baseline units; do not subtract these columns.",
            "",
            "The combined reduction toward the approximately 180-unit goal and its numerical "
            "shortfall remain UNVERIFIED. Run 39505581812 has no downloadable artifacts; "
            "merge run 39512253651 failed and does not establish a successful aggregate. "
            "Current PR CI validates the repair but cannot reconstruct a matching historical "
            "before/after comparison. No local full-suite or verify pipeline was run.",
            "",
            "This is permitted partial delivery under issue #843's explicit partial-PR "
            "acceptance clause: preserve the 25 discriminating tests and publish their "
            "mutation proof, with the coverage shortfall explicitly unverified. No coverage "
            "configuration or thresholds are changed. Excluded residual selftest lines: "
            "capability_effectiveness 910/925 are borrowed fake-connection close methods; "
            "`_edge_rows` closes only connections it opened, so these methods are unreachable; redirect_shadow "
            "1383 is the else arm after a bare report is required to raise; "
            "strategy_experiment 1037 asserts False only if invalid single-arm normalization "
            "fails to raise. These are local line exclusions from the completion claim, "
            "not proven combined-unit exclusions. The other four targets have no local "
            "missing-line exclusions; their combined branch results remain unverified.",
            "",
        ]
    )
    report = "\n".join(lines) + "\n"
    receipt_json = json.dumps(receipts, indent=2, sort_keys=True) + "\n"
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPT_JSON.write_text(receipt_json, encoding="utf-8")
    REPORT_MD.write_text(report, encoding="utf-8")


def git_head() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", help="Interpreter (default: repository-selected or anaconda)")
    parser.add_argument(
        "--write-report", action="store_true", help="run baseline+mutations and write evidence"
    )
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--mutations-only", action="store_true")
    parser.add_argument("--limit", type=int, help="run only the first N mutation specs (debug)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.limit is not None and args.write_report:
        parser.error("--limit cannot be combined with --write-report (evidence would be partial)")

    python = resolve_python(args.python)
    if args.baseline_only:
        result = run_focused_baseline(python)
        print(json.dumps(result, indent=2))
        return 0 if result["exit_code"] == 0 else 1

    if args.mutations_only:
        receipts = run_mutations(python, limit=args.limit)
        print(json.dumps(receipts, indent=2))
        return 0

    if args.write_report:
        baseline = run_focused_baseline(python)
        if baseline["exit_code"] != 0:
            print(json.dumps(baseline, indent=2))
            return baseline["exit_code"]
        receipts = run_mutations(python, limit=args.limit)
        write_report(python=python, baseline=baseline, receipts=receipts, git_head=git_head())
        print(f"wrote {REPORT_MD} and {RECEIPT_JSON}")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
