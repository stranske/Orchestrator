#!/usr/bin/env python3
"""watch.py - conservative stall-watcher for local delegates and experiment lanes.

The orchestrator used to monitor detached agents with ad-hoc log tails and git
diff checks. This module hardens that pattern into a read-only classifier:
running / progress / stalled / exited / missing, plus advisory root-cause hints
and a recommended next action.

It never kills processes, releases claims, or mutates live state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import redirect_policy
import redirect_plan

DEFAULT_STALE_SECONDS = 600
LOG_TAIL_BYTES = 800
WORKTREE_SCAN_DEPTH = 3
DEFAULT_DRIFT_MAX_FILES = 25
DEFAULT_DRIFT_MAX_TOP_LEVEL_DIRS = 5

_HIGH_RISK_PATHS: list[tuple[str, re.Pattern[str]]] = [
    ("workflow", re.compile(r"^\.github/workflows/")),
    ("github-action", re.compile(r"^\.github/actions/")),
    (
        "dependency-lock",
        re.compile(r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock)$"),
    ),
    (
        "project-config",
        re.compile(
            r"(^|/)(pyproject\.toml|package\.json|requirements.*\.txt|tox\.ini|setup\.cfg)$"
        ),
    ),
    (
        "automation-config",
        re.compile(r"(^|/)(sync-manifest\.ya?ml|renovate\.json|dependabot\.ya?ml)$"),
    ),
]
_TASK_FORBIDDEN_PATHS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "testgen": [
        (
            "testgen_gate_infra",
            re.compile(
                r"(^|/)Orchestrator/(testgen_gate|testgen_lane)\.py$|(^|/)(testgen_gate|testgen_lane)\.py$"
            ),
        ),
    ],
}

_HINT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "auth",
        re.compile(
            r"(401|403|unauthorized|authentication failed|not authenticated|"
            r"invalid.{0,20}(api.?key|token)|oauth|permission denied)",
            re.I,
        ),
    ),
    (
        "rate_limit",
        re.compile(r"(rate.?limit|429|quota exceeded|too many requests|capacity exceeded)", re.I),
    ),
    (
        "fatal",
        re.compile(
            r"(fatal error|traceback \(most recent|segmentation fault|panic:|unhandled exception)",
            re.I,
        ),
    ),
    (
        "network",
        re.compile(
            r"(connection (refused|reset|timed out)|name or service not known|network is unreachable)",
            re.I,
        ),
    ),
]


def tail_text(text: str, limit: int) -> str:
    if not text:
        return ""
    return text[-limit:] if len(text) > limit else text


def read_log_tail(path: str | Path | None, limit: int = LOG_TAIL_BYTES) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    return (
        data[-limit:].decode(errors="replace")
        if len(data) > limit
        else data.decode(errors="replace")
    )


def pid_alive(pid: int | None) -> bool | None:
    """True/False when pid is known; None when pid is unknown."""
    if pid is None:
        return None
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def file_age_seconds(path: str | Path | None, now: float | None = None) -> float | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (now if now is not None else time.time()) - mtime)


def _git(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, check=False
    )


def _is_git_worktree(worktree: str | Path | None) -> bool:
    if not worktree:
        return False
    wt = Path(worktree)
    res = _git(wt, ["rev-parse", "--is-inside-work-tree"])
    return res.returncode == 0 and res.stdout.strip() == "true"


def git_signals(
    worktree: str | Path | None, base_ref: str | None, errors: list[str] | None = None
) -> dict:
    """Return git progress signals; empty values when no git worktree is available."""
    empty = {
        "status_short": "",
        "uncommitted_diff_stat": "(none)",
        "committed_diff_stat": "(none)",
        "has_worktree_changes": False,
        "git_available": False,
    }
    if not worktree:
        return empty
    wt = Path(worktree)
    if not _is_git_worktree(wt):
        return empty

    status = _git(wt, ["status", "--short"]).stdout.rstrip("\n")
    uncommitted = ""
    committed = ""
    if base_ref:
        uncommitted_res = _git(wt, ["diff", "--stat", base_ref])
        if uncommitted_res.returncode == 0:
            uncommitted = uncommitted_res.stdout.strip()
        elif errors is not None:
            errors.append(
                f"git diff --stat failed for base_ref '{base_ref}': "
                f"{tail_text(uncommitted_res.stderr.strip(), 240)}"
            )
        committed_res = _git(wt, ["diff", "--stat", base_ref, "HEAD"])
        if committed_res.returncode == 0:
            committed = committed_res.stdout.strip()
        elif errors is not None:
            errors.append(
                f"git diff --stat HEAD failed for base_ref '{base_ref}': "
                f"{tail_text(committed_res.stderr.strip(), 240)}"
            )
    return {
        "status_short": status,
        "uncommitted_diff_stat": uncommitted or "(none)",
        "committed_diff_stat": committed or "(none)",
        "has_worktree_changes": bool(status or uncommitted or committed),
        "git_available": True,
    }


def _status_paths(status_short: str) -> list[str]:
    paths: list[str] = []
    for line in status_short.splitlines():
        if len(line) < 3:
            continue
        path = (line[3:] if len(line) >= 4 and line[2] == " " else line[2:]).strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.append(path)
    return paths


def changed_paths(
    worktree: str | Path | None,
    base_ref: str | None,
    errors: list[str] | None = None,
) -> list[str]:
    """Return changed paths from git status and optional base diff."""
    if not worktree:
        return []
    wt = Path(worktree)
    if not _is_git_worktree(wt):
        return []
    status = _git(wt, ["status", "--short", "--untracked-files=all"]).stdout.rstrip("\n")
    paths: set[str] = set(_status_paths(status))
    if base_ref:
        diff_res = _git(wt, ["diff", "--name-only", base_ref])
        if diff_res.returncode == 0:
            paths.update(p.strip() for p in diff_res.stdout.splitlines() if p.strip())
        elif errors is not None:
            errors.append(
                f"git diff --name-only failed for base_ref '{base_ref}': "
                f"{tail_text(diff_res.stderr.strip(), 240)}"
            )
        committed_res = _git(wt, ["diff", "--name-only", base_ref, "HEAD"])
        if committed_res.returncode == 0:
            paths.update(p.strip() for p in committed_res.stdout.splitlines() if p.strip())
        elif errors is not None:
            errors.append(
                f"git diff --name-only HEAD failed for base_ref '{base_ref}': "
                f"{tail_text(committed_res.stderr.strip(), 240)}"
            )
    return sorted(paths)


def _top_level(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else path


def _under_expected(path: str, expected: list[str]) -> bool:
    normalized = path.strip("/").lower()
    for item in expected:
        trimmed = item.strip()
        if trimmed in {".", "/"}:
            return True
        exp = trimmed.strip("/").lower()
        if not exp:
            continue
        if normalized == exp or normalized.startswith(exp.rstrip("/") + "/"):
            return True
    return False


def drift_signals(
    worktree: str | Path | None,
    base_ref: str | None = None,
    *,
    expected_paths: list[str] | None = None,
    task_type: str = "",
    max_files: int = DEFAULT_DRIFT_MAX_FILES,
    max_top_level_dirs: int = DEFAULT_DRIFT_MAX_TOP_LEVEL_DIRS,
    errors: list[str] | None = None,
) -> dict:
    """Conservative, read-only semantic drift hints from changed paths.

    This intentionally flags only structural risks. It does not decide correctness and it
    does not call an LLM. Medium/high findings mean the orchestrator should inspect the
    diff against the task scope before letting the delegate continue.
    """
    paths = changed_paths(worktree, base_ref, errors=errors)
    expected = [p for p in (expected_paths or []) if p.strip()]
    findings: list[dict] = []
    top_dirs = sorted({_top_level(path) for path in paths})
    task_key = (task_type or "").strip().lower()

    forbidden_hits = []
    for kind, pattern in _TASK_FORBIDDEN_PATHS.get(task_key, []):
        for path in paths:
            if pattern.search(path):
                forbidden_hits.append({"kind": kind, "path": path})
    if forbidden_hits:
        findings.append(
            {
                "kind": "forbidden_task_paths",
                "severity": "high",
                "detail": f"{task_key} lane changed read-only Orchestrator gate/helper path(s)",
                "paths": [hit["path"] for hit in forbidden_hits[:10]],
            }
        )

    if expected:
        unexpected = [path for path in paths if not _under_expected(path, expected)]
        if unexpected:
            findings.append(
                {
                    "kind": "unexpected_paths",
                    "severity": "medium",
                    "detail": f"{len(unexpected)} changed path(s) outside expected scope",
                    "paths": unexpected[:10],
                }
            )

    high_risk_hits = []
    for path in paths:
        if expected and _under_expected(path, expected):
            continue
        for kind, pattern in _HIGH_RISK_PATHS:
            if pattern.search(path):
                high_risk_hits.append({"kind": kind, "path": path})
                break
    if high_risk_hits:
        findings.append(
            {
                "kind": "high_risk_paths",
                "severity": "medium",
                "detail": "changed high-risk automation/dependency/config path(s) outside explicit expected scope",
                "paths": [hit["path"] for hit in high_risk_hits[:10]],
            }
        )

    if len(paths) > max_files or len(top_dirs) > max_top_level_dirs:
        findings.append(
            {
                "kind": "broad_churn",
                "severity": "medium" if len(paths) <= max_files * 2 else "high",
                "detail": f"{len(paths)} changed path(s) across {len(top_dirs)} top-level area(s)",
                "top_level": top_dirs[:12],
            }
        )

    severity_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    severity = "none"
    for finding in findings:
        if severity_rank.get(finding["severity"], 0) > severity_rank[severity]:
            severity = finding["severity"]
    return {
        "severity": severity,
        "changed_paths": paths[:50],
        "changed_path_count": len(paths),
        "top_level_dirs": top_dirs,
        "expected_paths": expected,
        "findings": findings,
    }


def worktree_activity_age(worktree: str | Path | None, now: float | None = None) -> float | None:
    """Seconds since newest shallow non-.git file mtime. This is only a weak activity hint."""
    if not worktree:
        return None
    root = Path(worktree)
    if not root.is_dir():
        return None
    ts = now if now is not None else time.time()
    newest: float | None = None
    if _is_git_worktree(root):
        status = _git(root, ["status", "--short", "--untracked-files=all"]).stdout.rstrip("\n")
        for rel in _status_paths(status):
            path = root / rel
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
        if newest is not None:
            return max(0.0, ts - newest)
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            parts = path.relative_to(root).parts
            if ".git" in parts or len(parts) > WORKTREE_SCAN_DEPTH:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    except OSError:
        return None
    return None if newest is None else max(0.0, ts - newest)


def hints_from_log(log_tail: str) -> list[dict]:
    """Advisory root-cause hints from obvious log-tail patterns."""
    if not log_tail:
        return []
    hints = []
    for kind, pattern in _HINT_PATTERNS:
        match = pattern.search(log_tail)
        if not match:
            continue
        line_start = log_tail.rfind("\n", 0, match.start()) + 1
        line_end = log_tail.find("\n", match.end())
        line = log_tail[line_start:] if line_end == -1 else log_tail[line_start:line_end]
        hints.append({"kind": kind, "detail": tail_text(line.strip() or match.group(0), 160)})
    return hints


def recommended_action(state: str, hints: list[dict]) -> str:
    if state == "exited":
        return "collect"
    if state in {"running", "progress"}:
        return "wait"
    if state == "stalled":
        hint_kinds = {h.get("kind") for h in hints}
        return "redirect" if hint_kinds & {"auth", "rate_limit", "fatal", "network"} else "inspect"
    return "inspect"


def _action_for_signals(
    state: str, hints: list[dict], signals: dict, drift: dict | None = None
) -> str:
    if any(
        (finding or {}).get("kind") == "forbidden_task_paths"
        for finding in (drift or {}).get("findings", [])
    ):
        return "inspect"
    if state in {"running", "progress"} and (drift or {}).get("severity") in {"medium", "high"}:
        return "inspect"
    if (
        state == "stalled"
        and signals.get("pid_alive") is None
        and signals.get("has_worktree_changes")
    ):
        return "collect"
    return recommended_action(state, hints)


# item 16(k) (2026-07-08): A2A TaskState vocabulary (Agent2Agent protocol) on every watch report.
# "Stuck waiting on auth/input" must not read as "failed" downstream (keepalive, redirect corpus,
# outcome labels) — the distinction is the whole value of the vocabulary. Signatures come from the
# log tail the report already carries; the mapping is deliberately coarse and fail-safe (unknown).
_A2A_AUTH_RE = re.compile(
    r"(unauthorized|forbidden|\b40[13]\b|rate.?limit|oauth|log ?in required|authentication|"
    r"credential|token expired)",
    re.I,
)
_A2A_INPUT_RE = re.compile(
    r"(\[y/n\]|\(y/n\)|press enter|awaiting (user )?input|proceed\?|continue\?|"
    r"waiting for (approval|confirmation))",
    re.I,
)


def a2a_state(state: str, log_tail: str = "") -> str:
    tail = (log_tail or "")[-2000:]
    if state in ("progress", "running"):
        return "working"
    if state == "stalled":
        if _A2A_AUTH_RE.search(tail):
            return "auth-required"
        if _A2A_INPUT_RE.search(tail):
            return "input-required"
        return "working"
    if state == "exited":
        # terminal-done, NOT success — outcomes/durability judge success separately
        return "completed"
    return "unknown"


def _finish_report(report: dict, attempt_history: list[dict] | None) -> dict:
    report["a2a_state"] = a2a_state(report.get("state") or "", report.get("log_tail") or "")
    report["policy_decision"] = redirect_policy.decide(report, attempt_history)
    report["redirect_plan"] = redirect_plan.plan(report)
    return report


def _all_known_ages_stale(ages: list[float | None], stale_seconds: int) -> bool:
    known = [age for age in ages if age is not None]
    return bool(known) and all(age >= stale_seconds for age in known)


def classify_lane(
    *,
    agent: str = "",
    target: str = "",
    lane: str = "",
    task_type: str = "",
    pid: int | None = None,
    log: str | Path | None = None,
    worktree: str | Path | None = None,
    base_ref: str | None = None,
    expected_paths: list[str] | None = None,
    attempt_history: list[dict] | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    now: float | None = None,
) -> dict:
    """Classify one watched lane without mutating live state."""
    # Credit the capability HERE, not only in main(). The heartbeat used to live solely on the CLI
    # path, but every production driver calls this function directly — redirect_sweep.py:473,
    # watch_sweep.py and exp_abcd.py:737 — so the activation audit reported `heartbeat_off_path`
    # and stall-watcher read as unable to fire while running fine. A heartbeat stranded on a path
    # nothing takes is the same defect as no heartbeat at all: the capability can never accrue
    # evidence of its own usefulness, and eventually reads as dead code.
    _capability_heartbeat()
    if pid is None and not log and not worktree:
        report = {
            "agent": agent,
            "target": target,
            "lane": lane,
            "task_type": task_type,
            "pid": pid,
            "log": "",
            "worktree": "",
            "base_ref": base_ref or "",
            "expected_paths": expected_paths or [],
            "state": "missing",
            "recommended_action": "inspect",
            "signals": {},
            "log_tail": "",
            "hints": [],
            "errors": ["need at least one of --pid, --log, or --worktree"],
        }
        return _finish_report(report, attempt_history)

    alive = pid_alive(pid)
    log_path = Path(log) if log else None
    wt_path = Path(worktree) if worktree else None
    errors: list[str] = []

    if log_path and not log_path.exists():
        errors.append(f"log not found: {log_path}")
    if wt_path and not wt_path.exists():
        errors.append(f"worktree not found: {wt_path}")

    tail = read_log_tail(log_path)
    hints = hints_from_log(tail)
    if alive is False:
        state = "exited"
        git = git_signals(wt_path, base_ref, errors=errors)
        drift = drift_signals(
            wt_path,
            base_ref,
            expected_paths=expected_paths,
            task_type=task_type,
            errors=errors,
        )
        signals = {"pid_alive": False, **git}
        report = {
            "agent": agent,
            "target": target,
            "lane": lane,
            "task_type": task_type,
            "pid": pid,
            "log": str(log_path) if log_path else "",
            "worktree": str(wt_path) if wt_path else "",
            "base_ref": base_ref or "",
            "expected_paths": expected_paths or [],
            "state": state,
            "recommended_action": _action_for_signals(state, hints, signals, drift),
            "signals": signals,
            "drift": drift,
            "log_tail": tail,
            "hints": hints,
            "errors": errors,
        }
        return _finish_report(report, attempt_history)

    log_age = file_age_seconds(log_path, now=now)
    wt_age = worktree_activity_age(wt_path, now=now)
    git = git_signals(wt_path, base_ref, errors=errors)
    drift = drift_signals(
        wt_path,
        base_ref,
        expected_paths=expected_paths,
        task_type=task_type,
        errors=errors,
    )
    log_recent = log_age is not None and log_age < stale_seconds
    worktree_recent = wt_age is not None and wt_age < stale_seconds
    has_progress = bool(log_recent or (git["has_worktree_changes"] and worktree_recent))

    signals = {
        "pid_alive": alive,
        "log_age_seconds": log_age,
        "worktree_age_seconds": wt_age,
        "log_recent": log_recent,
        "worktree_recent": worktree_recent,
        "stale_seconds": stale_seconds,
        **git,
    }

    if errors and pid is None and not tail and not git["git_available"]:
        state = "missing"
    elif has_progress:
        state = "progress"
    elif alive is True and _all_known_ages_stale([log_age, wt_age], stale_seconds):
        state = "stalled"
    elif alive is None and _all_known_ages_stale([log_age, wt_age], stale_seconds):
        state = "stalled"
    else:
        state = "running"

    report = {
        "agent": agent,
        "target": target,
        "lane": lane,
        "task_type": task_type,
        "pid": pid,
        "log": str(log_path) if log_path else "",
        "worktree": str(wt_path) if wt_path else "",
        "base_ref": base_ref or "",
        "expected_paths": expected_paths or [],
        "state": state,
        "recommended_action": _action_for_signals(state, hints, signals, drift),
        "signals": signals,
        "drift": drift,
        "log_tail": tail,
        "hints": hints,
        "errors": errors,
    }
    return _finish_report(report, attempt_history)


def format_human(report: dict) -> str:
    lines = [
        f"agent={report.get('agent') or '-'} target={report.get('target') or '-'} "
        f"pid={report.get('pid') if report.get('pid') is not None else '-'}",
        f"state={report.get('state')} recommended_action={report.get('recommended_action')}",
    ]
    signals = report.get("signals") or {}
    if signals:
        parts = []
        if signals.get("pid_alive") is not None:
            parts.append(f"pid_alive={signals['pid_alive']}")
        if signals.get("log_age_seconds") is not None:
            parts.append(f"log_age={int(signals['log_age_seconds'])}s")
        if signals.get("worktree_age_seconds") is not None:
            parts.append(f"worktree_age={int(signals['worktree_age_seconds'])}s")
        if signals.get("has_worktree_changes"):
            parts.append("git_changes=yes")
        lines.append("signals: " + " ".join(parts))
        if signals.get("status_short"):
            lines.append(f"status: {signals['status_short']}")
        if signals.get("uncommitted_diff_stat"):
            lines.append(f"uncommitted: {signals['uncommitted_diff_stat']}")
        if signals.get("committed_diff_stat"):
            lines.append(f"committed: {signals['committed_diff_stat']}")
    drift = report.get("drift") or {}
    if drift and drift.get("severity") != "none":
        lines.append(
            f"drift: severity={drift.get('severity')} "
            f"changed_paths={drift.get('changed_path_count', 0)}"
        )
        for finding in drift.get("findings") or []:
            lines.append(f"drift[{finding.get('kind')}]: {finding.get('detail')}")
    policy = report.get("policy_decision") or {}
    if policy:
        lines.append(
            f"policy: action={policy.get('action')} confidence={policy.get('confidence')} "
            f"reason={policy.get('reason')}"
        )
    plan = report.get("redirect_plan") or {}
    if plan:
        lines.append(
            f"plan: action={plan.get('action')} steps={len(plan.get('steps') or [])} "
            f"requires_confirmation={plan.get('requires_confirmation')}"
        )
    for hint in report.get("hints") or []:
        lines.append(f"hint[{hint.get('kind')}]: {hint.get('detail')}")
    for error in report.get("errors") or []:
        lines.append(f"error: {error}")
    tail = (report.get("log_tail") or "").strip()
    if tail:
        lines.append("--- log tail ---")
        lines.append(tail)
    return "\n".join(lines)


def _selftest() -> None:
    import tempfile

    now = 1_700_000_000.0
    stale = 600

    assert tail_text("abcdef", 3) == "def"
    assert read_log_tail("/no/such/file.log") == ""
    assert pid_alive(None) is None
    assert pid_alive(0) is False
    assert pid_alive(os.getpid()) is True
    assert recommended_action("exited", []) == "collect"
    assert recommended_action("progress", []) == "wait"
    assert recommended_action("running", []) == "wait"
    assert recommended_action("stalled", []) == "inspect"
    assert recommended_action("stalled", [{"kind": "auth"}]) == "redirect"
    assert hints_from_log("HTTP 401 Unauthorized")[0]["kind"] == "auth"
    assert hints_from_log("Error: rate limit exceeded")[0]["kind"] == "rate_limit"

    with tempfile.TemporaryDirectory(prefix="watch-selftest-") as tmp:
        root = Path(tmp)
        log = root / "agent.log"
        log.write_text("working\n")
        os.utime(log, (now - 30, now - 30))
        wt = root / "repo"
        subprocess.run(["git", "init", str(wt)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(wt), "config", "user.email", "watch@example.test"], check=True
        )
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Watcher"], check=True)
        (wt / "tracked.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(wt), "add", "tracked.py"], check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "base"],
            check=True,
            capture_output=True,
            text=True,
        )
        base = "HEAD"

        running = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            stale_seconds=stale,
            now=now,
        )
        assert running["state"] == "progress", running
        assert running["recommended_action"] == "wait", running

        os.utime(log, (now - 900, now - 900))
        (wt / "tracked.py").write_text("x = 2\n")
        with_changes = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            stale_seconds=stale,
            now=now,
        )
        assert with_changes["state"] == "progress", with_changes
        assert with_changes["signals"]["has_worktree_changes"] is True, with_changes

        deep = wt / "src" / "components" / "auth" / "handlers" / "login.py"
        deep.parent.mkdir(parents=True)
        deep.write_text("LOGIN = True\n")
        os.utime(deep, (now - 30, now - 30))
        deep_progress = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            stale_seconds=stale,
            now=now,
        )
        assert deep_progress["state"] == "progress", deep_progress
        subprocess.run(
            ["git", "-C", str(wt), "clean", "-fd", "src"],
            check=True,
            capture_output=True,
            text=True,
        )

        subprocess.run(["git", "-C", str(wt), "checkout", "--", "tracked.py"], check=True)
        os.utime(wt / "tracked.py", (now - 900, now - 900))
        stalled = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            stale_seconds=stale,
            now=now,
        )
        assert stalled["state"] == "stalled", stalled
        assert stalled["recommended_action"] == "inspect", stalled
        repeat_stall = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            stale_seconds=stale,
            now=now,
            attempt_history=[stalled, stalled],
        )
        assert repeat_stall["policy_decision"]["action"] == "decompose", repeat_stall[
            "policy_decision"
        ]
        assert repeat_stall["redirect_plan"]["action"] == "decompose", repeat_stall["redirect_plan"]
        assert repeat_stall["redirect_plan"]["requires_confirmation"] is True, repeat_stall[
            "redirect_plan"
        ]

        (wt / "tracked.py").write_text("x = 333\n")
        os.utime(wt / "tracked.py", (now - 900, now - 900))
        old_diff = classify_lane(
            pid=None,
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            task_type="testgen",
            stale_seconds=stale,
            now=now,
        )
        assert old_diff["state"] == "stalled", old_diff
        assert old_diff["recommended_action"] == "collect", old_diff
        assert old_diff["redirect_plan"]["task_type"] == "testgen", old_diff["redirect_plan"]
        subprocess.run(["git", "-C", str(wt), "checkout", "--", "tracked.py"], check=True)

        # Drift detection is advisory: obvious workflow/config churn asks the orchestrator to inspect.
        (wt / ".github" / "workflows").mkdir(parents=True)
        workflow = wt / ".github" / "workflows" / "ci.yml"
        workflow.write_text("name: ci\n")
        drifted = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            stale_seconds=stale,
            now=now,
        )
        assert drifted["drift"]["severity"] == "medium", drifted["drift"]
        assert drifted["recommended_action"] == "inspect", drifted
        assert drifted["policy_decision"]["action"] == "inspect", drifted["policy_decision"]
        assert drifted["redirect_plan"]["action"] == "inspect", drifted["redirect_plan"]
        assert "drift:" in format_human(drifted), format_human(drifted)
        assert "plan:" in format_human(drifted), format_human(drifted)

        scoped = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            expected_paths=[".github/workflows"],
            stale_seconds=stale,
            now=now,
        )
        assert scoped["drift"]["severity"] == "none", scoped["drift"]
        root_scoped = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            expected_paths=["."],
            stale_seconds=stale,
            now=now,
        )
        assert root_scoped["drift"]["severity"] == "none", root_scoped["drift"]

        gate_file = wt / "Orchestrator" / "testgen_gate.py"
        gate_file.parent.mkdir(parents=True)
        gate_file.write_text("# changed by mistake\n")
        forbidden = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref=base,
            task_type="testgen",
            expected_paths=["."],
            stale_seconds=stale,
            now=now,
        )
        assert forbidden["drift"]["severity"] == "high", forbidden["drift"]
        assert forbidden["recommended_action"] == "inspect", forbidden
        assert any(
            finding["kind"] == "forbidden_task_paths" for finding in forbidden["drift"]["findings"]
        ), forbidden["drift"]
        subprocess.run(
            ["git", "-C", str(wt), "clean", "-fd", "Orchestrator"],
            check=True,
            capture_output=True,
            text=True,
        )

        for i in range(6):
            d = wt / f"area{i}"
            d.mkdir()
            (d / "file.txt").write_text("x\n")
        broad = drift_signals(wt, base, max_files=3, max_top_level_dirs=3)
        assert broad["severity"] in {"medium", "high"}, broad

        bad_base = classify_lane(
            pid=os.getpid(),
            log=str(log),
            worktree=str(wt),
            base_ref="origin/nope",
            stale_seconds=stale,
            now=now,
        )
        assert any("base_ref 'origin/nope'" in err for err in bad_base["errors"]), bad_base

        exited = classify_lane(pid=999_999_999, log=str(log), now=now)
        assert exited["state"] == "exited", exited
        assert exited["recommended_action"] == "collect", exited

    missing = classify_lane()
    assert missing["state"] == "missing" and missing["errors"], missing
    assert missing["a2a_state"] == "unknown", missing
    # 16(k): A2A vocabulary — stalled-on-auth/input is NOT "failed"; exited is terminal-done.
    assert a2a_state("running") == "working" and a2a_state("progress") == "working"
    assert a2a_state("stalled", "HTTP 401 Unauthorized — token expired") == "auth-required"
    assert a2a_state("stalled", "Proceed? [y/N]") == "input-required"
    assert a2a_state("stalled", "still compiling, slow box") == "working"
    assert a2a_state("exited") == "completed" and a2a_state("missing") == "unknown"
    print(
        "watch.py selftest: OK (tail/age/pid, hints, classify progress/stalled/exited/missing, "
        "drift, A2A state mapping)"
    )


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this infrastructure capability ran. Infra is never ROUTED to — it runs as part
    of the tick — so it records use at its own entrypoint. Lazy import, never raises, and inert
    outside an active tick (ORCH_CAPABILITY_HEARTBEATS). (2026-08-09)"""
    try:
        import capabilities

        capabilities.production_heartbeat("stall-watcher", event_type, ref="watch.main")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _capability_heartbeat()
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        _selftest()
        return 0

    parser = argparse.ArgumentParser(
        description="Conservative stall-watcher for detached local delegates."
    )
    parser.add_argument("--agent", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--lane", default="")
    parser.add_argument("--task-type", default="")
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--log", default="")
    parser.add_argument("--worktree", default="")
    parser.add_argument("--base-ref", default="")
    parser.add_argument(
        "--expected-path",
        action="append",
        default=[],
        help="expected in-scope path prefix; repeatable",
    )
    parser.add_argument(
        "--attempt-history-json",
        default="",
        help="JSON file containing prior watch reports for this target",
    )
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    attempt_history = None
    if args.attempt_history_json:
        try:
            attempt_history = json.loads(Path(args.attempt_history_json).read_text())
        except FileNotFoundError:
            print(f"error: history file not found: {args.attempt_history_json}", file=sys.stderr)
            return 1

    report = classify_lane(
        agent=args.agent,
        target=args.target,
        lane=args.lane,
        task_type=args.task_type,
        pid=args.pid,
        log=args.log or None,
        worktree=args.worktree or None,
        base_ref=args.base_ref or None,
        expected_paths=args.expected_path,
        attempt_history=attempt_history,
        stale_seconds=args.stale_seconds,
    )
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(format_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
