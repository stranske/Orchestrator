"""Recover the Gate coverage comparison for src/capability_advisor.py.

PR #650 reported a local drop from 94 missing lines to 61 and cited a CI baseline of 68.
Those figures are different measurements. This module reads coverage.py JSON, keeps uncovered
statements and uncovered branches apart, and refuses to add an unmeasured branch count as zero.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TARGET = "src/capability_advisor.py"
GATE_COMMAND = "python3 src/verify.py --coverage"
TARGET_REDUCTION = 60

RECORDED_ACCEPTANCE = {
    "check_date": "2026-11-05",
    "python": "Python 3.14.9 (/opt/hostedtoolcache/Python/3.14.9/x64/bin/python3)",
    "anaconda": "/opt/anaconda3/bin/python3 is absent on this runner",
    "attempts": 1,
    "elapsed_seconds": 0.64,
    "stop_rule": (
        "Stop after 2 attempts or 120 seconds elapsed, whichever comes first. "
        "Do not start an attempt after 90 seconds have already elapsed."
    ),
    "task": "add unit tests for the retry helper",
    "ledger": (
        "temporary ORCH_LOCAL_RUNTIME with no capabilities.json; "
        "load_declared therefore saw no rows"
    ),
    "outcome": {
        "capability_ids": [],
        "confidence": "high",
        "dispatch_ready_count": 0,
        "reason": "classified as testgen, but no capability declares a trigger for that work",
        "task_types": ["testgen"],
        "useful": False,
    },
}

# Issue #647's own selection text, created 2026-10-16T21:09:09Z. A fresh git recount of
# fix/revert touches and churn on src/capability_advisor.py through 2026-10-16 is 24 and 46,
# not 22 and 45, so that recount is not substituted for the issue text.
SELECTION = {
    "source": "https://github.com/stranske/Orchestrator/issues/647",
    "created_at": "2026-10-16T21:09:09Z",
    "order": ["escaped defects", "churn", "uncovered mass"],
    "selected": [
        {
            "path": TARGET,
            "rank": 1,
            "reported_uncovered_units": 68,
            "escaped_defect_signals": 22,
            "churn": 45,
            "evidence": "fix commit touched this file",
        }
    ],
    "excluded_in_flight": [
        "src/adversarial.py",
        "src/capabilities.py",
        "src/capability_admission.py",
        "src/runtime_ac.py",
    ],
    "recount_not_substituted": {
        "window": "2026-04-19..2026-10-16",
        "path": TARGET,
        "fix_or_revert_touches": 24,
        "escaped_weight": 24.0,
        "churn": 46,
    },
}

LOCAL_COHORT = {
    "before_missing": 94,
    "after_missing": 61,
    "before_lines": "1499/1593",
    "after_lines": "1532/1593",
    "source": "PR #650 body: local selftest plus the related-pytest cohort",
    "comparable_to_gate": False,
}

# Why each statement the recovered head artifact still missed was outside #650's tests.
# Later commits added tests for several of these paths. Those tests are not in the d477734
# artifact, and this module does not add them into the 29-statement Gate reduction.
_IMPORT_EXCEPT = (
    "The except returns only when the import itself fails. PR #650 patches the helper after "
    "the import has succeeded, so this statement does not run."
)
_RANK_EXCEPT = (
    "advise swallows a capability_propensity.rank failure. PR #650's consult tests replace "
    "advise, so this except does not run."
)
_RESTORE_ELSE = (
    "This is the else that writes a pre-existing SURFACE_BINDINGS entry back. PR #650 does "
    "not pre-seed that key, so the selftest takes the pop branch."
)
_FRONT_DOOR = (
    "This statement is inside _selftest_front_door, after the ledger-row gate. CI's "
    "bootstrapped ledger has no offload, codemod-campaign, or testgen-lane row, so "
    "ledger_rows_absent returns first. PR #650 does not seed those rows."
)
_EMPTY_TABLE = (
    "This statement is the body of the KNOWN_UNCONSULTED loop. At d477734 that table is "
    "intentionally empty, so the loop body does not run. A temporary entry also fails the "
    "selftest's stale-table assert unless the surface is bound and unconsulted."
)


def head_statement_limit(line: int) -> str:
    """The specific reason this head statement stayed uncovered in the recovered Gate artifact."""
    reasons = {
        201: f"_dispatcher_task_type_capability import except. {_IMPORT_EXCEPT}",
        202: f"return {{}} on that import failure. {_IMPORT_EXCEPT}",
        533: f"_record_fact_missing import except. {_IMPORT_EXCEPT}",
        534: f"return 0 on that import failure. {_IMPORT_EXCEPT}",
        700: f"classification-miss path. {_RANK_EXCEPT}",
        701: f"pass on that rank failure. {_RANK_EXCEPT}",
        935: f"classified path. {_RANK_EXCEPT}",
        938: f"pass on that rank failure. The lines between the except and this pass are comments. {_RANK_EXCEPT}",
        1684: (
            "_promoted_index writes a binding_promotion event. PR #650 never passes a ledger "
            "event whose metadata source is binding_promotion."
        ),
        2206: (
            "consult_phases_guarded finally-handler except (ValueError, OSError). PR #650 "
            "makes signal.signal raise before the alarm is armed, so this except is skipped."
        ),
        2207: "pass on that cleanup failure. Same unentered except as line 2206.",
        2211: "_consult_expired raises TimeoutError. PR #650 never calls the SIGALRM handler.",
        2228: (
            "format_phase_consult success-row branch. PR #650 renders phase rows that carry "
            "an error, which takes the other branch."
        ),
        2703: (
            "detect_observable_surface returns once the marker count reaches "
            "_SURFACE_MAX_MARKERS (6). PR #650's scan finds three markers."
        ),
        2790: (
            "evaluate_precondition when the required repo fact has no registered probe. "
            "PR #650 registers an exploding probe, which is the failure branch."
        ),
        3013: f"_record_auto_declines import except. {_IMPORT_EXCEPT}",
        3014: f"return 0 on that import failure. {_IMPORT_EXCEPT}",
        3815: f"restore SURFACE_BINDINGS['t-howto']. {_RESTORE_ELSE}",
        3913: f"restore SURFACE_BINDINGS['t-concept']. {_RESTORE_ELSE}",
        4208: f"restore SURFACE_BINDINGS['t-contra']. {_RESTORE_ELSE}",
        4545: f"restore SURFACE_BINDINGS['t-empty']. {_RESTORE_ELSE}",
        4606: f"restore SURFACE_BINDINGS['t-surface']. {_RESTORE_ELSE}",
        5115: f"restore SURFACE_BINDINGS['t-precond']. {_RESTORE_ELSE}",
        5252: f"FIX: assert over KNOWN_UNCONSULTED. {_EMPTY_TABLE}",
        5261: f"--surface contradiction assert. {_EMPTY_TABLE}",
        5578: (
            "main calls argparse's error when argv has no task. PR #650's CLI test always "
            "passes a task or --consult-tick-phases."
        ),
    }
    if 3940 <= line <= 3960:
        return f"Line {line} of the front-door cases. {_FRONT_DOOR}"
    try:
        return reasons[line]
    except KeyError as exc:
        raise KeyError(f"no limit recorded for uncovered statement {line}") from exc


def load_slices(path: Path | None = None) -> dict[str, Any]:
    fixture = (
        path or Path(__file__).resolve().parent / "fixtures" / "issue1102_advisor_gate_slices.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))


def coverage_payload(measurement: dict[str, Any]) -> dict[str, Any]:
    """Wrap one recovered file slice in the coverage.py JSON shape the Gate uploads."""
    missing = list(measurement["missing_lines"])
    summary: dict[str, Any] = {
        "num_statements": measurement["num_statements"],
        "covered_lines": measurement["covered_lines"],
        "missing_lines": len(missing),
    }
    file_row: dict[str, Any] = {"missing_lines": missing, "summary": summary}
    if measurement["branch_coverage"] is True:
        branches = list(measurement.get("missing_branch_locations") or [])
        summary["missing_branches"] = measurement["missing_branches"]
        file_row["missing_branches"] = branches
    return {
        "meta": {
            "format": 3,
            "version": measurement["coverage_version"],
            "branch_coverage": measurement["branch_coverage"],
            "timestamp": measurement["timestamp"],
        },
        "files": {TARGET: file_row},
    }


def statement_and_branch_gap(payload: dict[str, Any], path: str = TARGET) -> dict[str, Any]:
    """Uncovered statements and uncovered branches from one coverage.py JSON payload.

    When ``meta.branch_coverage`` is false, branches are unmeasured. They are not reported as
    zero and they are not added to the statement count.
    """
    meta = payload.get("meta") or {}
    files = payload.get("files") or {}
    if path not in files:
        raise KeyError(path)
    file_row = files[path]
    summary = file_row.get("summary") or {}
    missing_lines = file_row.get("missing_lines")
    if not isinstance(missing_lines, list):
        raise ValueError(f"{path} has no missing_lines list")
    if summary.get("missing_lines") != len(missing_lines):
        raise ValueError(
            f"{path} summary missing_lines {summary.get('missing_lines')} "
            f"!= list length {len(missing_lines)}"
        )
    branch_measured = meta.get("branch_coverage") is True
    if branch_measured:
        locations = file_row.get("missing_branches")
        count = summary.get("missing_branches")
        if not isinstance(locations, list) or not isinstance(count, int):
            raise ValueError(f"{path} claims branch coverage without branch locations")
        if count != len(locations):
            raise ValueError(f"{path} missing_branches {count} != {len(locations)} locations")
        uncovered_branches: int | None = count
        branch_locations: list | None = locations
        uncovered_units: int | None = len(missing_lines) + count
    else:
        uncovered_branches = None
        branch_locations = None
        uncovered_units = None
    return {
        "path": path,
        "coverage_version": meta.get("version"),
        "branch_coverage": meta.get("branch_coverage"),
        "branches_measured": branch_measured,
        "statements_total": summary.get("num_statements"),
        "statements_covered": summary.get("covered_lines"),
        "uncovered_statements": len(missing_lines),
        "uncovered_statement_locations": list(missing_lines),
        "uncovered_branches": uncovered_branches,
        "uncovered_branch_locations": branch_locations,
        "uncovered_units": uncovered_units,
    }


def gate_statement_reduction(before: dict[str, Any], after: dict[str, Any]) -> int:
    """Missing-statement reduction for two Gate payloads of the same file.

    Both sides must leave branches unmeasured, or both must measure them. A second baseline,
    including PR #650's local 94/61 cohort, is not an argument of this function.
    """
    if before["path"] != after["path"]:
        raise ValueError("refusing to subtract gaps for different files")
    if before["branches_measured"] != after["branches_measured"]:
        raise ValueError("refusing to subtract gaps with different branch configuration")
    if before["coverage_version"] != after["coverage_version"]:
        raise ValueError("refusing to subtract gaps from different coverage versions")
    if before["branches_measured"]:
        raise ValueError("branch-measured Gate payloads are not the configuration CI uploaded")
    return int(before["uncovered_statements"]) - int(after["uncovered_statements"])


def acceptance_env(runtime: Path, repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["ORCH_STATE_DIR"] = str(runtime / "state")
    env["ORCH_LOCAL_RUNTIME"] = str(runtime / "runtime")
    env["ORCH_CAPABILITIES_PATH"] = str(runtime / "runtime" / "capabilities.json")
    env["PYTHONPATH"] = str(repo / "src")
    return env


def acceptance_script() -> str:
    task = RECORDED_ACCEPTANCE["task"]
    return (
        "import json\n"
        "import capability_advisor as advisor\n"
        f"result = advisor.advise({task!r}, repository='', record=False)\n"
        "projected = {\n"
        "    'useful': result['useful'],\n"
        "    'confidence': result['confidence'],\n"
        "    'task_types': result['task_types'],\n"
        "    'capability_ids': [row['capability_id'] for row in result['capabilities']],\n"
        "    'dispatch_ready_count': result['dispatch_ready_count'],\n"
        "    'reason': result['reason'],\n"
        "}\n"
        "print(json.dumps(projected, sort_keys=True))\n"
    )


def run_acceptance(runtime: Path, repo: Path, *, timeout: float = 90) -> dict[str, Any]:
    """Run the focused advise command in a fresh interpreter. Bound by ``timeout`` seconds."""
    if timeout > 120:
        raise ValueError("the focused acceptance command must finish in under two minutes")
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", acceptance_script()],
        cwd=repo,
        env=acceptance_env(runtime, repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return {"elapsed_seconds": elapsed, "outcome": json.loads(proc.stdout)}


def render_report(slices: dict[str, Any] | None = None) -> str:
    """Inspectable comparison of the recovered base and head Gate artifacts."""
    slices = slices or load_slices()
    measurements = slices["measurements"]
    gaps = {
        name: statement_and_branch_gap(coverage_payload(row)) for name, row in measurements.items()
    }
    reduction = gate_statement_reduction(gaps["base"], gaps["head"])
    merge_reduction = gate_statement_reduction(gaps["merge_base"], gaps["head"])
    head_lines = gaps["head"]["uncovered_statement_locations"]
    selected = SELECTION["selected"][0]
    lines = [
        "# Issue #1102 — capability_advisor Gate coverage evidence",
        "",
        "Recorded before any edit to `src/capability_advisor.py` on 2026-11-05. This report",
        "uses the recovered Gate `coverage.json` artifacts. It does not mix them with PR #650's",
        "local cohort.",
        "",
        "## Real-input acceptance",
        "",
        f"- Check date: {RECORDED_ACCEPTANCE['check_date']}",
        f"- Python: {RECORDED_ACCEPTANCE['python']}",
        f"- {RECORDED_ACCEPTANCE['anaconda']}",
        f"- Attempts: {RECORDED_ACCEPTANCE['attempts']}",
        f"- Elapsed: {RECORDED_ACCEPTANCE['elapsed_seconds']} seconds",
        f"- Stop rule: {RECORDED_ACCEPTANCE['stop_rule']}",
        f"- Ledger: {RECORDED_ACCEPTANCE['ledger']}",
        "- Command: set `ORCH_STATE_DIR`, `ORCH_LOCAL_RUNTIME`, and `ORCH_CAPABILITIES_PATH`",
        "  to a fresh temporary directory, set `PYTHONPATH=src`, and run",
        "  `python3 -c` with `capability_advisor.advise('add unit tests for the retry helper',",
        "  repository='', record=False)`, printing `useful`, `confidence`, `task_types`,",
        "  `capability_ids`, `dispatch_ready_count`, and `reason`.",
        f"- Outcome: `{json.dumps(RECORDED_ACCEPTANCE['outcome'], sort_keys=True)}`",
        "",
        "## Revisions and ranking",
        "",
        f"- Ranking source: {SELECTION['source']} ({SELECTION['created_at']})",
        f"- Order: {', then '.join(SELECTION['order'])}",
        (
            f"- Selected: `{selected['path']}` rank {selected['rank']}, "
            f"{selected['reported_uncovered_units']} reported uncovered units, "
            f"{selected['escaped_defect_signals']} escaped-defect signals, "
            f"churn {selected['churn']} ({selected['evidence']})"
        ),
        f"- Left alone: {', '.join(SELECTION['excluded_in_flight'])}",
        (
            "- A git recount over 2026-04-19..2026-10-16 on this path is "
            f"{SELECTION['recount_not_substituted']['fix_or_revert_touches']} fix/revert touches "
            f"and churn {SELECTION['recount_not_substituted']['churn']}. It is not the selection "
            "record."
        ),
        "- PR #650 tests at head: `tests/test_capability_advisor_coverage.py` (13 tests).",
        f"- Base `{measurements['base']['revision']}`: {measurements['base']['role']}.",
        f"- Merge base `{measurements['merge_base']['revision']}`: {measurements['merge_base']['role']}.",
        f"- Head `{measurements['head']['revision']}`: {measurements['head']['role']}.",
        "- Squash merge onto main: `d6e0d40057c60b19481c39f7f85015ad5c6c8731`.",
        "",
        "## Measurement configuration",
        "",
        f"- Gate command: `{slices['gate_command']}`",
        f"- CI shard command: `{slices['ci_shard_command']}`",
        f"- CI aggregate command: `{slices['ci_aggregate_command']}`",
        f"- Artifact name: `{slices['artifact_name']}` file `{slices['artifact_file']}`",
        (
            "- coverage.py "
            f"{measurements['base']['coverage_version']}, "
            f"branch_coverage={measurements['base']['branch_coverage']}, "
            f"source={slices['config']['source']}, parallel={slices['config']['parallel']}, "
            f"relative_files={slices['config']['relative_files']}, "
            f"config `{slices['config']['file']}` `[{slices['config']['section']}]` "
            f"branch={slices['config']['branch']}"
        ),
        "",
        "## Comparable Gate results for src/capability_advisor.py",
        "",
        "| Revision | CI run | Statements | Covered | Uncovered statements | Uncovered branches |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for name in ("base", "merge_base", "head"):
        row = measurements[name]
        gap = gaps[name]
        branches = (
            "unmeasured" if gap["uncovered_branches"] is None else str(gap["uncovered_branches"])
        )
        lines.append(
            f"| `{row['revision'][:12]}` | {row['ci_run']} | {gap['statements_total']} | "
            f"{gap['statements_covered']} | {gap['uncovered_statements']} | {branches} |"
        )
    lines += [
        "",
        f"- Base artifact: {measurements['base']['artifact']}",
        f"- Merge-base artifact: {measurements['merge_base']['artifact']}",
        f"- Head artifact: {measurements['head']['artifact']}",
        (
            f"- Missing statements: {gaps['base']['uncovered_statements']} at base and at "
            f"merge base, {gaps['head']['uncovered_statements']} at head."
        ),
        f"- Statement reduction, base to head: {reduction}.",
        f"- Statement reduction, merge base to head: {merge_reduction}.",
        "- Uncovered branch locations: none. `meta.branch_coverage` is false, so branches were",
        "  not measured. That is not a measured zero and it is not part of the reduction.",
        f"- Uncovered statement locations at base: {gaps['base']['uncovered_statement_locations']}",
        f"- Uncovered statement locations at head: {head_lines}",
        "",
        "## Target",
        "",
        (
            f"Issue #647 asked for about {TARGET_REDUCTION} fewer uncovered units. The comparable "
            f"Gate statement reduction is {reduction}. The shortfall on that measure is "
            f"{TARGET_REDUCTION - reduction} statements. No branch reduction is claimed."
        ),
        "",
        (
            f"PR #650's local cohort went from {LOCAL_COHORT['before_missing']} missing "
            f"({LOCAL_COHORT['before_lines']}) to {LOCAL_COHORT['after_missing']} missing "
            f"({LOCAL_COHORT['after_lines']}), a drop of "
            f"{LOCAL_COHORT['before_missing'] - LOCAL_COHORT['after_missing']}. "
            "The Gate base is 1525/1593 with 68 missing, so the local denominator is a different "
            "measurement. It is not added to the Gate reduction and it is not the 68-unit baseline."
        ),
        "",
        "Coverage delivered by #650, on the Gate statement measure: 29. Coverage added by this",
        "successor is not in these artifacts and is not included in that 29.",
        "",
        "The statements still missing at head, and why #650's tests did not execute them:",
        "",
    ]
    for line in head_lines:
        lines.append(f"- `{TARGET}:{line}` — {head_statement_limit(line)}")
    lines.append("")
    return "\n".join(lines)
