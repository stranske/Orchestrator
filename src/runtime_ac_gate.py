#!/usr/bin/env python3
"""Shared runtime-AC gate enforcement for closer PR paths.

This module owns the common requirement detection and active gate execution used
by both autonomous ticks and terminal merge guards. Specs live on local disk by
default so the Dropbox code checkout never holds runtime state.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import capabilities
import feedback
import runtime_ac

RUNTIME_AC_REQUIRED_LABELS = {
    "runtime-ac",
    "runtime-verification",
    "acceptance-criteria",
    "verification-spec",
    "verification-plan",
    "ac-checks",
    "runtime-checks",
}
DEFAULT_RUNTIME_AC_SPEC_DIR = Path.home() / ".codex" / "orchestrator" / "runtime-ac"
DEFAULT_BACKLOG_JSON = (
    Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff")) / "backlog.json"
)
DEFAULT_CRON_LOG = (
    Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
    / "orchestrator-cron.log"
)


def target_slug(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", target).strip("_")


def spec_path(
    target: str,
    *,
    spec_dir: str | Path | None = None,
    env: Mapping[str, Any] | None = None,
) -> Path:
    source_env: Mapping[str, Any] = os.environ if env is None else env
    state_root = Path(source_env.get("ORCH_STATE_DIR") or DEFAULT_RUNTIME_AC_SPEC_DIR.parent)
    root = Path(spec_dir or source_env.get("ORCH_RUNTIME_AC_SPEC_DIR") or state_root / "runtime-ac")
    return root / f"{target_slug(target)}.json"


def _all_label_names(item: dict) -> list[str]:
    """The item's own labels PLUS its source issue's labels.

    The gate is closer-only and closer items carry PR labels; verification-relevant metadata lives
    on the source ISSUE. Verified 2026-08-20: 0 PRs in the fleet carry a `risk:*` label, and none
    carries a runtime-AC label either, so reading `labels` alone made the gate unreachable by
    anything except a pre-existing spec file. `backlog.build_backlog` attaches `source_labels`.
    """
    out = []
    for key in ("labels", "source_labels"):
        for label in item.get(key) or []:
            out.append(str(label.get("name") if isinstance(label, dict) else label).strip())
    return out


def required(item: dict, path: Path) -> bool:
    labels = {label.lower() for label in _all_label_names(item)}
    normalized = labels | {label.split(":", 1)[1].strip() for label in labels if ":" in label}
    return bool(normalized & RUNTIME_AC_REQUIRED_LABELS) or _enforcing_spec(path)


def _enforcing_spec(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return (
            json.loads(path.read_text(encoding="utf-8")).get("verification", {}).get("shadow_only")
            is not True
        )
    except (OSError, ValueError, AttributeError):
        # A broken existing hard-gate spec remains required and fails closed.
        return True


def eligibility(item: dict, path: Path) -> dict:
    """Return exact, target-local evidence that makes a closer gate eligible."""
    raw_labels = _all_label_names(item)
    matched_labels = []
    for raw in raw_labels:
        lowered = raw.lower()
        normalized = lowered.split(":", 1)[1].strip() if ":" in lowered else lowered
        if normalized in RUNTIME_AC_REQUIRED_LABELS:
            matched_labels.append(raw)
    by_label = bool(matched_labels)
    by_spec = _enforcing_spec(path)
    source = (
        "label+spec"
        if by_label and by_spec
        else "label" if by_label else "spec" if by_spec else "none"
    )
    refs = list(matched_labels)
    if by_spec:
        refs.append(str(path))
    return {
        "required": by_label or by_spec,
        "source": source,
        "refs": refs,
    }


def author_keepalive_spec(
    repo: str, pr: dict, run_id: str, *, issue_fetch_fn, spec_dir=None, env=None
) -> dict:
    """Materialize once at the existing ingest edge, with no merge/outcome writes."""
    target = f"{repo}#{int(pr['number'])}"
    path = spec_path(target, spec_dir=spec_dir, env=env)
    if path.exists():
        return {"status": "existing", "spec_authored": False, "spec_path": str(path)}
    link = re.search(
        r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+"
        r"(?:(?P<ref_repo>[\w.-]+/[\w.-]+)?#(?P<ref_number>[1-9]\d*)"
        r"|https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/"
        r"(?P<url_number>[1-9]\d*))\b",
        str(pr.get("body") or ""),
        re.I,
    )
    if not link:
        return {"status": "unlinked", "spec_authored": False}
    issue_repo = link["ref_repo"] or link["url_repo"] or repo
    issue_number = int(link["ref_number"] or link["url_number"])
    issue = f"{issue_repo}#{issue_number}"
    try:
        body = issue_fetch_fn(issue_repo, issue_number)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("linked issue body unavailable")
        spec = runtime_ac.author_issue_spec(issue, body, target=target)
        spec["verification"]["repo"] = repo
        spec["verification"]["observed_head_sha"] = pr.get("headRefOid")
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        )
        staged = Path(stream.name)
        try:
            with stream:
                json.dump(spec, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    staged, path
                )  # Exclusive, atomic publication; never overwrite an operator spec.
            except FileExistsError:
                return {"status": "existing", "spec_authored": False, "spec_path": str(path)}
        finally:
            staged.unlink(missing_ok=True)
        event = feedback.record_runtime_ac_gate_event(
            target=target,
            gate_status="materialized",
            required=False,
            dry_run=True,
            eligibility_source="spec",
            eligibility_refs=[str(path)],
            spec_path=str(path),
            spec_hash=spec_sha256(path),
            spec_path_matches_target=True,
            blocking=False,
            closer_run_id=run_id,
            materialization_source="keepalive_issue_author",
            materialization_status="spec_authored",
            shadow_only=True,
            spec_authored=True,
            observed_head_sha=pr.get("headRefOid"),
            terminal_reason="shadow_spec_authored",
        )
        return {
            "status": "authored",
            "spec_authored": True,
            "spec_path": str(path),
            "gate_event": event,
        }
    except Exception as exc:
        return {"status": "error", "spec_authored": False, "error": str(exc)}


SHADOW_BUDGET_FLAG = "ORCH_RUNTIME_AC_SHADOW_BUDGET"
DEFAULT_EXECUTION_BUDGET = 3  # per ingest or sweep when the flag is unset; the tick exports 12


def execution_budget(env=None, *, default: int = DEFAULT_EXECUTION_BUDGET) -> int:
    """Disposable exact-head executions one ingest or one sweep may run; one flag for both.

    Three a day measured 9 of 166 shadow specs in a week (112 PRs refused for budget on
    2026-10-16 alone), so orchestrate.sh exports ORCH_RUNTIME_AC_SHADOW_BUDGET=12. A caller
    without the flag, tests included, keeps the old bound.
    """
    resolved = os.environ if env is None else env
    raw = str(resolved.get(SHADOW_BUDGET_FLAG, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(0, value)


REOPEN_FLAG = "ORCH_RUNTIME_AC_REOPEN"
REOPEN_EXECUTED_FLOOR = 10


def reopen_policy(env=None, summary: dict | None = None) -> dict:
    """Whether a post-merge FAIL may reopen its issue: decided by evidence, not by a date.

    `ORCH_RUNTIME_AC_REOPEN=1` forces on and `=0` is the kill switch. Unset or `auto` allows
    reopening once at least REOPEN_EXECUTED_FLOOR rechecks have EXECUTED (PASS plus FAIL): ten
    runs that reached a verdict show the runner works, and a FAIL caused by the runner itself is
    already classified UNRUNNABLE (commands_disabled, tool_error, ...) rather than FAIL, so the
    FAILs that remain are about the delivery. Until 2026-10-17 this was a flag that expired on a
    calendar date, which timed the decision instead of the evidence (owner, 2026-10-17).
    """
    resolved = os.environ if env is None else env
    raw = str(resolved.get(REOPEN_FLAG, "auto") or "auto").strip().lower()
    section = summary or {}
    executed = int(section.get("passed") or 0) + int(section.get("failed") or 0)
    if raw in {"1", "on", "true", "yes"}:
        mode, allowed = "forced_on", True
    elif raw in {"0", "off", "false", "no"}:
        mode, allowed = "off", False
    else:
        mode, allowed = "auto", executed >= REOPEN_EXECUTED_FLOOR
    return {"mode": mode, "allowed": allowed, "executed": executed, "floor": REOPEN_EXECUTED_FLOOR}


def shadow_summary(*, now: int, events=None) -> dict:
    """Count distinct PRs; retain unknown execution/audit evidence as unknown."""
    try:
        rows = (
            events
            if events is not None
            else feedback.runtime_ac_gate_events(cutoff_ts=now - 7 * 86400, limit=10000)
        )
    except Exception as exc:
        return {"error": str(exc)}
    rows = [row for row in rows if row.get("shadow_only")]
    targets = {row["target"] for row in rows}
    authored = {row["target"] for row in rows if row.get("spec_authored")}
    latest = {}
    for row in sorted(rows, key=lambda row: row.get("created_ts", 0)):
        if row.get("gate_status") == "executed":
            latest[row["target"]] = row
    executed = set(latest)
    # Report observed causes within the weekly window, counting each PR once
    # per reason. A PR may have several causes; any execution removes it from
    # this unmeasured population, even if a later ingest runs out of budget.
    unmeasured_reasons: dict[str, set[str]] = {}
    for row in rows:
        if row["target"] not in executed and row.get("gate_status") == "planned":
            reason = row.get("terminal_reason") or "unrecorded"
            unmeasured_reasons.setdefault(reason, set()).add(row["target"])
    failed = {target for target, row in latest.items() if row.get("verifier_verdict") == "FAIL"}
    # The LATEST observation per unmeasured PR: a window that mixes a pre-fix cause with a
    # later budget refusal reports what stands now beside every cause ever recorded.
    latest_planned: dict[str, str] = {}
    for row in sorted(rows, key=lambda row: row.get("created_ts", 0)):
        if row["target"] not in executed and row.get("gate_status") == "planned":
            latest_planned[row["target"]] = row.get("terminal_reason") or "unrecorded"
    latest_counts: dict[str, int] = {}
    for reason in latest_planned.values():
        latest_counts[reason] = latest_counts.get(reason, 0) + 1
    merged_unmet = {
        row["target"]
        for row in rows
        if row["target"] in failed and row.get("downstream_merged") is True
    }
    audited = [row for row in latest.values() if row.get("false_fail_audit") is not None]
    return {
        "prs": len(targets),
        "specs_authored": len(authored),
        "executed": len(executed),
        "would_fail": len(failed),
        "merged_with_unmet": len(merged_unmet),
        "would_fail_targets": sorted(failed),
        "merged_with_unmet_targets": sorted(merged_unmet),
        "latest_reason_per_pr": dict(sorted(latest_counts.items())),
        "false_fail_count": sum(row["false_fail_audit"] is True for row in audited),
        "audit_sample": sorted({row["target"] for row in audited}),
        "execution_unmeasured": len(targets - executed),
        "execution_unmeasured_reasons": {
            reason: len(prs) for reason, prs in sorted(unmeasured_reasons.items())
        },
    }


def observe_shadow_spec(
    target: str,
    run_id: str,
    *,
    spec_dir=None,
    env=None,
    worktree=None,
    head_sha=None,
    unavailable_reason: str | None = None,
) -> dict:
    """Shadow execution needs the existing opt-in and an exact PR checkout.

    An ingest's mirror checkout is never a substitute for the delivered PR.
    No outcome is patched, and no result feeds merge guard.
    """
    resolved = os.environ if env is None else env
    path = spec_path(target, spec_dir=spec_dir, env=resolved)
    if not path.exists():
        return {"status": "missing_spec", "blocks": False}
    spec = runtime_ac.parse_spec_json(path.read_text(encoding="utf-8"))
    if spec.get("verification", {}).get("shadow_only") is not True:
        return {"status": "operator_spec", "blocks": False}
    status, reason, verdict, audit = "planned", "shadow_execution_disabled", None, None
    evidence = []
    if env_flag(resolved, "ORCH_RUN_RUNTIME_AC"):
        reason = unavailable_reason or "shadow_exact_checkout_missing"
        # The authored head is historical context; every observation independently
        # pins the current PR checkout, without rewriting the once-authored spec.
        if unavailable_reason is None and worktree and head_sha:
            current = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if current.returncode == 0 and current.stdout.strip() == head_sha:
                spec.setdefault("runtime_context", {})["worktree"] = str(worktree)
                run = runtime_ac.run_verification(
                    spec,
                    confirm_run=True,
                    allow_command_checks=env_flag(resolved, "ORCH_RUNTIME_AC_ALLOW_COMMANDS"),
                )
                verdict = run["gate"]["verdict"]
                status, reason = "executed", "shadow_only_no_merge_authority"
                results = [
                    row for row in run["check_results"] if "pytest" in str(row.get("command", ""))
                ]
                passed = [
                    row
                    for row in results
                    if re.search(r"\b[1-9]\d* passed\b", str(row.get("stdout_tail", "")))
                    and not re.search(
                        r"\b[1-9]\d* (?:failed|errors?)\b", str(row.get("stdout_tail", ""))
                    )
                ]
                if passed:
                    audit = any(row["status"] == "FAIL" for row in passed)
                # Only identifiers/counts travel to the canonical event, never raw stdout.
                evidence = [
                    {
                        "command": row.get("command"),
                        "status": row.get("status"),
                        "returncode": row.get("returncode"),
                        "passing_named_test": row in passed,
                    }
                    for row in results
                ]
    event = feedback.record_runtime_ac_gate_event(
        target=target,
        gate_status=status,
        required=False,
        dry_run=True,
        eligibility_source="spec",
        eligibility_refs=[str(path)],
        spec_path=str(path),
        spec_hash=spec_sha256(path),
        spec_path_matches_target=True,
        blocking=False,
        closer_run_id=run_id,
        verifier_verdict=verdict,
        shadow_only=True,
        observed_head_sha=head_sha,
        false_fail_audit=audit,
        audit_evidence=evidence,
        terminal_reason=reason,
    )
    return {"status": status, "blocks": False, "gate_event": event}


def _worktree_interpreter(worktree: str | Path) -> str:
    """Use a repository venv, provisioning one from this checkout when absent.

    Disposable git worktrees do not inherit the canonical checkout's untracked
    environment. Bootstrap with the scheduler's interpreter, but never run checks
    with it or expose its site-packages to the repository.
    """
    root = Path(worktree).resolve()
    for name in (".venv", "venv"):
        environment = root / name
        interpreter = environment / "bin" / "python"
        if interpreter.is_file():
            return str(interpreter)
        if environment.exists() or environment.is_symlink():
            raise RuntimeError(f"selected repository interpreter is unavailable: {interpreter}")
    environment = root / ".venv"
    interpreter = environment / "bin" / "python"
    metadata = root / "pyproject.toml"
    project = tomllib.loads(metadata.read_text(encoding="utf-8")) if metadata.is_file() else {}
    distribution = any(key in project for key in ("project", "build-system")) or bool(
        project.get("tool", {}).get("poetry")
    )
    distribution = distribution or any(
        (root / name).is_file() for name in ("setup.py", "setup.cfg")
    )
    requirements = ["pytest"]
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        if (root / name).is_file():
            requirements.extend(["-r", str(root / name)])
    if distribution:
        extras = project.get("project", {}).get("optional-dependencies", {})
        selected = sorted(set(extras) & {"dev", "test", "tests"})
        requirements.append(".[" + ",".join(selected) + "]" if selected else ".")
    setup_env = os.environ.copy()
    # Scheduler install destinations can redirect pip outside this venv or force
    # an unsupported user install. Retain index settings for repository dependencies.
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        setup_env.pop(key, None)
    # Build backends and dependency installers can launch Python by name too.
    # Keep their children in this venv, just like the subsequent command checks.
    setup_env["PATH"] = str(interpreter.parent) + os.pathsep + setup_env.get("PATH", "")
    try:
        for command in (
            [sys.executable, "-m", "venv", str(environment)],
            [
                str(interpreter),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *requirements,
            ],
        ):
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=setup_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode:
                raise RuntimeError("repository environment setup failed")
    except Exception:
        # A partially installed environment must never be selected by a retry.
        shutil.rmtree(environment, ignore_errors=True)
        raise
    return str(interpreter)


def _deliberate_break_proof(row):
    """Read the runner's bounded result; old complete display JSON is legacy only."""
    if "deliberate_break_result" in row:
        proof = row["deliberate_break_result"]
        normalized = runtime_ac.bounded_deliberate_break_result(proof)
        return normalized if normalized is not None and normalized == proof else None
    text = row.get("stdout_tail")
    if not isinstance(text, str) or len(text) > runtime_ac.TAIL_CHARS:
        return None
    try:
        proof = json.loads(text)
    except ValueError:
        return None
    return runtime_ac.bounded_deliberate_break_result(proof)


def recheck_merged(
    spec: dict,
    merged_worktree: str | Path,
    *,
    allow_command_checks: bool | None = None,
    merge_ci_green: bool = False,
) -> dict:
    """Run the existing named-check/break runner; manual or missing evidence is unknown.

    The caller provisions and pins the merge commit. Never mutate the authored spec
    or consume this observation as merge authority. An UNRUNNABLE result carries its
    `reason` (one of UNRUNNABLE_REASONS), because "cannot run" has four causes and only one
    of them — `manual_only`, a spec with no executable check — is about the delivery.
    """
    allow = (
        post_merge_commands_allowed(os.environ)
        if allow_command_checks is None
        else bool(allow_command_checks)
    )
    interpreter = None
    try:
        current = copy.deepcopy(spec)
        context = current.setdefault("runtime_context", {})
        context["worktree"] = str(merged_worktree)
        interpreter = _worktree_interpreter(merged_worktree)
        context["python_interpreter"] = interpreter
        for criterion in current.get("acceptance_criteria", []):
            for check in criterion.get("checks", []):
                if check.get("type") == "deliberate_break":
                    # The merge parent is the pre-delivery implementation. origin/main
                    # can already equal the merge tip or advance while this runs.
                    check["base_ref"] = "HEAD^"
                    check["worktree"] = str(merged_worktree)
        run = runtime_ac.run_verification(
            current,
            confirm_run=True,
            allow_command_checks=allow,
            timeout=env_int(os.environ, "ORCH_RUNTIME_AC_TIMEOUT", 120),
        )
        checks = run["check_results"]
        executable = [r for r in checks if r.get("type") != "manual"]

        # A failed assertion is evidence of a broken delivery. A tool error,
        # skipped command, missing break or manual-only spec is not that evidence.
        def assertion_failed(row):
            output = "\n".join(str(row.get(key) or "") for key in ("stdout_tail", "stderr_tail"))
            return row.get("returncode") == 1 and bool(
                re.search(r"\b[1-9]\d* failed\b|\bFAILED \(failures=[1-9]\d*", output)
            )

        def delivery_failed(row):
            if row.get("status") != "FAIL" or environment_mismatch(row):
                return False
            if row.get("type") == "deliberate_break":
                proof = _deliberate_break_proof(row)
                # local_verify distinguishes a broken candidate from a hollow
                # comparison. Confirm the candidate's assertion failure too:
                # FAIL_BROKEN alone can also describe a failed test launcher.
                return (
                    isinstance(proof, dict)
                    and proof.get("verdict") == "FAIL_BROKEN"
                    and not proof.get("error")
                    and assertion_failed(proof.get("green") or {})
                )
            # pytest exit 1 with a failure count proves assertion failure. Exit 2
            # (collection/import), 4 (bad invocation), 5 (no tests) and launcher
            # failures must not reopen a delivered issue.
            return assertion_failed(row)

        def environment_mismatch(row):
            """Recognize the runner's child-Python import failure, not a product assertion."""
            if not merge_ci_green or row.get("status") != "FAIL":
                return False
            candidate = row
            if row.get("type") == "deliberate_break":
                proof = _deliberate_break_proof(row)
                if not isinstance(proof, dict) or proof.get("verdict") != "FAIL_BROKEN":
                    return False
                candidate = proof.get("green") or {}
            if not isinstance(candidate, dict) or not assertion_failed(candidate):
                return False
            output = "\n".join(
                str(candidate.get(key) or "") for key in ("stdout_tail", "stderr_tail")
            )
            # A real failed assertion wins when both signals occur in one test run. The
            # environment classification is only for the isolated child-launcher failure.
            if re.search(r"\bAssertionError\b|\bassert(?:ion)?\s", output, re.I):
                return False
            return bool(
                re.search(
                    r"CalledProcessError[^\n]*(?:python|Python)|"
                    r"(?:subprocess\.|sys\.executable)[\s\S]*ModuleNotFoundError",
                    output,
                    re.I,
                )
            )

        if any(delivery_failed(r) for r in executable):
            result = "FAIL"
        elif (
            executable
            and all(r.get("status") == "PASS" for r in executable)
            # Manual criteria cannot be supplied to a machine recheck, so the gate's own verdict
            # reads NEEDS_REVIEW for every spec that mixes named tests with manual items: all
            # three rechecks of 2026-10-16 ran their named tests green and were still
            # UNRUNNABLE. The recheck's question is the named test on the merged tip; a gate
            # verdict other than PASS or that manual NEEDS_REVIEW still keeps it from PASS.
            and run["gate"]["verdict"] in {"PASS", "NEEDS_REVIEW"}
        ):
            result = "PASS"
        else:
            result = "UNRUNNABLE"
        reason = None
        if result == "UNRUNNABLE":

            def hollow(row):
                if row.get("type") != "deliberate_break":
                    return False
                proof = _deliberate_break_proof(row)
                return isinstance(proof, dict) and proof.get("verdict") == "FAIL_HOLLOW"

            if not executable:
                reason = "manual_only"
            elif any(
                r.get("status") == "SKIP" and "allow-command-checks" in str(r.get("reason") or "")
                for r in executable
            ):
                reason = "commands_disabled"
            elif any(hollow(r) for r in executable):
                reason = "hollow_break"
            elif any(environment_mismatch(r) for r in executable):
                reason = "environment_mismatch"
            else:
                reason = "incomplete"
        return {
            "result": result,
            "reason": reason,
            "interpreter": context["python_interpreter"],
            "transcript": json.dumps(run, indent=2, default=str),
        }
    except Exception as exc:
        return {
            "result": "UNRUNNABLE",
            "reason": "tool_error",
            "interpreter": interpreter,
            "transcript": str(exc),
        }


def post_merge_summary() -> dict:
    """Read the existing event plane, including successful and unknown rechecks."""
    try:
        with feedback._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM completion_events "
                "WHERE producer='runtime_ac_gate' AND phase='post_merge_recheck'"
            ).fetchall()
    except Exception as exc:
        return {"error": str(exc)}
    results = []
    malformed = 0
    for row in rows:
        try:
            payload = json.loads(row[0])
            result = payload.get("runtime_ac_post_merge", {})
            if not isinstance(result, dict) or result.get("result") not in {
                "PASS",
                "FAIL",
                "UNRUNNABLE",
            }:
                raise ValueError("post-merge result is missing a valid execution verdict")
        except (ValueError, TypeError, AttributeError):
            malformed += 1
            continue
        results.append(result)
    reasons: dict[str, int] = {}
    for r in results:
        if r.get("result") == "UNRUNNABLE":
            key = str(r.get("reason") or "unclassified")
            reasons[key] = reasons.get(key, 0) + 1
    summary = {
        "rechecked": len(results),
        # PASS is printed, not implied: a drained line reads "passed N ... unrunnable 0".
        "passed": sum(r.get("result") == "PASS" for r in results),
        "failed": sum(r.get("result") == "FAIL" for r in results),
        "reopened": sum(r.get("reopened") is True for r in results),
        "unrunnable": sum(r.get("result") == "UNRUNNABLE" for r in results),
        # Every cause counted beside the total, so "unrunnable 3" cannot read as "3 broken
        # deliveries" when it means the runner's own switch was off.
        "unrunnable_reasons": dict(sorted(reasons.items())),
    }
    if malformed:
        summary["malformed"] = malformed
    return summary


def format_post_merge_summary(section: dict) -> str:
    if section.get("error"):
        return f"runtime-AC post-merge: unmeasured ({section['error']})"
    # The reopen policy is a function of the switch and this section's executed count; it is
    # computed here, where it is printed, so the summary dict keeps the shape its readers pin.
    policy = reopen_policy(os.environ, section)
    reasons = section.get("unrunnable_reasons") or {}
    reason_bit = (
        " (" + ", ".join(f"{name} {count}" for name, count in sorted(reasons.items())) + ")"
        if reasons
        else ""
    )
    return (
        f"runtime-AC post-merge: rechecked {section['rechecked']}, "
        f"passed {section.get('passed', 0)}, "
        f"failed {section['failed']}, reopened {section['reopened']}, "
        f"unrunnable {section['unrunnable']}{reason_bit}"
        + (f", malformed {section['malformed']}" if section.get("malformed") else "")
        + f", reopen {policy['mode']} (executed {policy['executed']} of {policy['floor']})"
    )


def format_shadow_summary(section: dict) -> str:
    if section.get("error"):
        return f"runtime-AC shadow: unmeasured — {section['error']}"
    sample = section["audit_sample"]
    audit = (
        f"{section['false_fail_count']} of {len(sample)} ({', '.join(sample)})"
        if sample
        else "unmeasured (0 audited PRs)"
    )
    reasons = section.get("execution_unmeasured_reasons", {})
    reason_text = (
        "; recorded reasons (PRs per reason): "
        + ", ".join(f"{reason} {count}" for reason, count in sorted(reasons.items()))
        if reasons
        else ""
    )
    targets = section.get("would_fail_targets") or []
    unmet = " (unmet ACs: " + ", ".join(targets) + ")" if targets else " (unmet ACs)"
    latest = section.get("latest_reason_per_pr") or {}
    latest_text = (
        "; latest unmeasured state (PRs): "
        + ", ".join(f"{reason} {count}" for reason, count in sorted(latest.items()))
        if latest and latest != reasons
        else ""
    )
    return (
        f"runtime-AC shadow: PRs {section['prs']}, specs authored {section['specs_authored']}, "
        f"executed {section['executed']}, would-FAIL {section['would_fail']}{unmet}, "
        f"merged with unmet ACs {section['merged_with_unmet']}, false-FAIL audit {audit}; "
        f"execution unmeasured {section['execution_unmeasured']}{reason_text}{latest_text}"
    )


def spec_sha256(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _latest_closer_run(target: str, latest_run_fn=None) -> tuple[str | None, str | None]:
    lookup = latest_run_fn or feedback.latest_run_id_for_target
    try:
        return lookup(target, mode="remote"), None
    except Exception as exc:
        return None, str(exc)


def _record_gate_event(
    *,
    target: str,
    status: str,
    required_gate: bool,
    dry_run: bool,
    eligibility_info: dict,
    path: Path,
    blocks: bool,
    closer_run_id: str | None,
    terminal_reason: str | None = None,
    verifier_run_id: str | None = None,
    verifier_verdict: str | None = None,
    materialization_source: str | None = None,
    materialization_status: str | None = None,
    materialization_run_id: str | None = None,
) -> dict:
    """Write gate telemetry fail-open so observability never changes gate safety."""
    try:
        return feedback.record_runtime_ac_gate_event(
            target=target,
            gate_status=status,
            required=required_gate,
            dry_run=dry_run,
            eligibility_source=eligibility_info.get("source") or "none",
            eligibility_refs=eligibility_info.get("refs") or [],
            spec_path=str(path),
            spec_hash=spec_sha256(path),
            spec_path_matches_target=path == spec_path(target, spec_dir=path.parent),
            blocking=blocks,
            terminal_reason=terminal_reason,
            closer_run_id=closer_run_id,
            verifier_run_id=verifier_run_id,
            verifier_verdict=verifier_verdict,
            materialization_source=materialization_source,
            materialization_status=materialization_status,
            materialization_run_id=materialization_run_id,
        )
    except Exception as exc:
        return {"recorded": False, "error": str(exc)}


def env_flag(env: Mapping[str, Any], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def env_int(env: Mapping[str, Any], name: str, default: int) -> int:
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return default


POST_MERGE_COMMANDS_FLAG = "ORCH_RUNTIME_AC_POST_MERGE_COMMANDS"
# Two of these are about the SPEC or the delivery and are said on the issue: `manual_only` (no
# executable check at all) and `hollow_break` (the deliberate break ran and the named test did not
# fail on the broken base, so the test proves nothing). The other three are about the runner —
# `commands_disabled`, `tool_error`, `incomplete` (launcher, collection or invocation failures the
# verdict already refuses to count) — and are recorded without a comment. An UNRUNNABLE with no
# reason (rows written before this field) is unknown and is commented as before.
UNRUNNABLE_REASONS = (
    "commands_disabled",
    "manual_only",
    "hollow_break",
    "tool_error",
    "incomplete",
    "environment_mismatch",
)
RUNNER_SIDE_UNRUNNABLE = frozenset(
    {"commands_disabled", "tool_error", "incomplete", "environment_mismatch"}
)


def post_merge_commands_allowed(env: Mapping[str, Any]) -> bool:
    """Command checks are ON by default in the post-merge recheck; `=0` is its own kill switch.

    The recheck exists to run the issue's NAMED TEST on the merged tip, in a disposable exact-head
    checkout, through `shlex.split` with no shell. The keepalive ingest's shadow execution keeps its
    separate `ORCH_RUNTIME_AC_ALLOW_COMMANDS` opt-in because it runs on open PRs the fleet is still
    writing. Measured 2026-10-16: the first three rechecks were UNRUNNABLE because the sweep's
    environment never set the ingest's flag — the mechanism's drain was a switch its own run never
    set, so every command check was SKIP and the verdict could never be PASS or FAIL.
    """
    return str(env.get(POST_MERGE_COMMANDS_FLAG, "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def gate_status(
    item: dict,
    *,
    dry_run: bool,
    env: Mapping[str, Any] | None = None,
    spec_dir: str | Path | None = None,
    run_fn=None,
    latest_run_fn=None,
    record_fn=None,
) -> dict | None:
    """Return and persist runtime-AC gate truth for closer PRs.

    The gate remains a hard opt-in safety gate: a required active closer blocks
    on a missing spec, disabled execution, errors, or any verdict except PASS.
    """
    if item.get("lane") != "closer":
        return None
    resolved_env: Mapping[str, Any] = os.environ if env is None else env
    target = str(item.get("target") or "")
    if not target:
        return None
    path = spec_path(target, spec_dir=spec_dir, env=resolved_env)
    eligibility_info = eligibility(item, path)
    closer_run_id, closer_lookup_error = _latest_closer_run(target, latest_run_fn)
    if not eligibility_info["required"]:
        _record_gate_event(
            target=target,
            status="skipped",
            required_gate=False,
            dry_run=dry_run,
            eligibility_info=eligibility_info,
            path=path,
            blocks=False,
            closer_run_id=closer_run_id,
            terminal_reason="closer_not_runtime_ac_eligible",
        )
        return None

    base = {
        "target": target,
        "spec_path": str(path),
        "spec_hash": spec_sha256(path),
        "required": True,
        "eligibility_source": eligibility_info["source"],
        "eligibility_refs": eligibility_info["refs"],
        "closer_run_id": closer_run_id,
    }
    if closer_lookup_error:
        base["closer_run_lookup_error"] = closer_lookup_error
    capabilities.production_heartbeat(
        "runtime-ac-checks",
        "match",
        ref=target,
        metadata={
            "spec_path": str(path),
            "spec_hash": base["spec_hash"],
            "eligibility_source": eligibility_info["source"],
        },
    )
    if dry_run:
        if path.exists():
            result = {**base, "status": "planned", "blocks": False}
            result["gate_event"] = _record_gate_event(
                target=target,
                status="planned",
                required_gate=True,
                dry_run=True,
                eligibility_info=eligibility_info,
                path=path,
                blocks=False,
                closer_run_id=closer_run_id,
                terminal_reason="dry_run_plan_only",
            )
            return result
        result = {
            **base,
            "status": "missing_spec",
            "blocks": False,
            "detail": "runtime AC is required, but no verification spec exists yet",
        }
        result["gate_event"] = _record_gate_event(
            target=target,
            status="missing_spec",
            required_gate=True,
            dry_run=True,
            eligibility_info=eligibility_info,
            path=path,
            blocks=False,
            closer_run_id=closer_run_id,
            terminal_reason="required_spec_not_materialized",
        )
        return result

    if not path.exists():
        result = {
            **base,
            "status": "missing_spec",
            "blocks": True,
            "detail": "runtime AC is required before this closer can proceed",
        }
        result["gate_event"] = _record_gate_event(
            target=target,
            status="missing_spec",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason="required_spec_not_materialized",
        )
        return result
    if not env_flag(resolved_env, "ORCH_RUN_RUNTIME_AC"):
        result = {
            **base,
            "status": "required_but_not_run",
            "blocks": True,
            "detail": "set ORCH_RUN_RUNTIME_AC=1 to execute the runtime AC gate for required closer PRs",
        }
        result["gate_event"] = _record_gate_event(
            target=target,
            status="required",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason="runtime_ac_execution_disabled",
        )
        return result

    run_fn = run_fn or runtime_ac.run_verification
    record_fn = record_fn or runtime_ac.record_gate_verdict
    try:
        spec = runtime_ac.parse_spec_json(path.read_text(encoding="utf-8"))
        declared_target = str((spec.get("verification") or {}).get("target") or "").strip()
        if declared_target and declared_target != target:
            raise ValueError(
                f"runtime AC spec target {declared_target!r} does not match closer target {target!r}"
            )
        capabilities.production_heartbeat(
            "runtime-ac-checks",
            "invocation",
            ref=target,
            metadata={"spec_path": str(path), "spec_hash": spec_sha256(path)},
        )
        run = run_fn(
            spec,
            confirm_run=True,
            allow_command_checks=env_flag(resolved_env, "ORCH_RUNTIME_AC_ALLOW_COMMANDS"),
            timeout=env_int(resolved_env, "ORCH_RUNTIME_AC_TIMEOUT", 120),
        )
        gate = run["gate"]
        verdict = gate.get("verdict")
        result = {
            **base,
            "status": "executed",
            "blocks": verdict != "PASS",
            "verdict": verdict,
            "verifier_verdict": gate.get("verifier_verdict"),
            "pass_ratio": gate.get("pass_ratio"),
            "result_count": gate.get("result_count"),
            "blocking": gate.get("blocking") or [],
            "needs_review": gate.get("needs_review") or [],
            "run_id": closer_run_id,
            "verifier_run_id": gate.get("verification_id"),
        }
        run_id = closer_run_id
        try:
            if run_id:
                result["feedback"] = record_fn(run_id, gate)
            else:
                result["feedback"] = {
                    "recorded": False,
                    "reason": "no remote run_id found for target",
                }
        except Exception as feedback_exc:
            result["feedback"] = {"recorded": False, "error": str(feedback_exc)}
        feedback_result = result.get("feedback")
        feedback_recorded = bool(run_id) and not (
            isinstance(feedback_result, dict) and feedback_result.get("recorded") is False
        )
        if verdict == "PASS" and feedback_recorded:
            capabilities.production_heartbeat(
                "runtime-ac-checks",
                "success",
                ref=run_id,
                metadata={"spec_path": str(path), "verdict": verdict},
            )
            capabilities.production_heartbeat(
                "runtime-ac-checks",
                "outcome",
                ref=run_id,
                metadata={"sink": "feedback.outcomes"},
            )
        result["gate_event"] = _record_gate_event(
            target=target,
            status="executed",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=verdict != "PASS",
            closer_run_id=run_id,
            verifier_run_id=gate.get("verification_id"),
            verifier_verdict=verdict,
            terminal_reason=(
                None if verdict == "PASS" else f"verifier_{str(verdict or 'unknown').lower()}"
            ),
        )
        return result
    except Exception as exc:
        result = {**base, "status": "failed", "blocks": True, "error": str(exc)}
        result["gate_event"] = _record_gate_event(
            target=target,
            status="error",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason="gate_execution_error",
        )
        return result


def materialize_range_spec(
    target: str,
    source: dict | str | Path,
    *,
    spec_dir: str | Path | None = None,
    producer_run_id: str | None = None,
) -> dict:
    """Validate and atomically install a range-lane spec at the gate's exact path.

    Target/repository attribution is fail-closed.  A Workflows spec can never be
    installed for a Pension target, even when the JSON itself is otherwise valid.
    Every terminal result is retained in the structured gate-event plane.
    """
    target = str(target or "").strip()
    path = spec_path(target, spec_dir=spec_dir)
    closer_run_id, lookup_error = _latest_closer_run(target)
    materialization_source = "inline"
    try:
        if not target or "#" not in target:
            raise ValueError("materialization target must be an exact owner/repo#number reference")
        if isinstance(source, dict):
            spec = json.loads(json.dumps(source))
        else:
            source_path = Path(source)
            materialization_source = str(source_path)
            spec = runtime_ac.parse_spec_json(source_path.read_text(encoding="utf-8"))

        verification = spec.get("verification") or {}
        declared_target = str(verification.get("target") or "").strip()
        declared_repo = str(verification.get("repo") or "").strip()
        target_repo = target.split("#", 1)[0]
        if declared_target != target:
            raise ValueError(
                f"spec_target_mismatch: expected {target!r}, found {declared_target or '<missing>'!r}"
            )
        if declared_repo and declared_repo != target_repo:
            raise ValueError(
                f"spec_repo_mismatch: expected {target_repo!r}, found {declared_repo!r}"
            )
        errors = runtime_ac.validate_spec(spec)
        if errors:
            raise ValueError("invalid_runtime_ac_spec: " + "; ".join(errors))

        encoded = json.dumps(spec, indent=2, sort_keys=True) + "\n"
        expected_hash = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        installed_hash = spec_sha256(path)
        if installed_hash != expected_hash:
            raise RuntimeError("installed_spec_hash_mismatch")

        eligibility_info = {"source": "spec", "refs": [str(path)]}
        event = _record_gate_event(
            target=target,
            status="materialized",
            required_gate=True,
            dry_run=False,
            eligibility_info=eligibility_info,
            path=path,
            blocks=False,
            closer_run_id=closer_run_id,
            terminal_reason=None,
            materialization_source=materialization_source,
            materialization_status="installed",
            materialization_run_id=producer_run_id,
        )
        return {
            "status": "materialized",
            "target": target,
            "spec_path": str(path),
            "spec_hash": installed_hash,
            "closer_run_id": closer_run_id,
            "closer_run_lookup_error": lookup_error,
            "producer_run_id": producer_run_id,
            "gate_event": event,
        }
    except Exception as exc:
        reason = str(exc)
        event = _record_gate_event(
            target=target or "invalid-target",
            status="materialization_failed",
            required_gate=True,
            dry_run=False,
            eligibility_info={"source": "none", "refs": []},
            path=path,
            blocks=True,
            closer_run_id=closer_run_id,
            terminal_reason=reason,
            materialization_source=materialization_source,
            materialization_status="not_installed",
            materialization_run_id=producer_run_id,
        )
        return {
            "status": "materialization_failed",
            "target": target,
            "spec_path": str(path),
            "terminal_reason": reason,
            "producer_run_id": producer_run_id,
            "gate_event": event,
        }


def _active_preflight(status: dict, env: Mapping[str, Any]) -> dict:
    if status.get("status") == "missing_spec":
        return {
            "status": "missing_spec",
            "blocks_active": True,
            "next_action": "create a runtime AC spec at the reported spec_path",
        }
    if status.get("status") == "planned" and not env_flag(env, "ORCH_RUN_RUNTIME_AC"):
        return {
            "status": "requires_env",
            "blocks_active": True,
            "next_action": "set ORCH_RUN_RUNTIME_AC=1 before active closer/merge execution",
        }
    if status.get("status") == "planned":
        return {
            "status": "ready_to_execute",
            "blocks_active": False,
            "next_action": "active execution would run the runtime AC spec",
        }
    return {
        "status": status.get("status") or "unknown",
        "blocks_active": bool(status.get("blocks")),
        "next_action": status.get("detail") or status.get("error") or "",
    }


def scan_items(
    items: list[dict],
    *,
    env: Mapping[str, Any] | None = None,
    spec_dir: str | Path | None = None,
) -> dict:
    """Read-only scan of backlog/closer items for runtime-AC gate coverage."""
    resolved_env: Mapping[str, Any] = os.environ if env is None else env
    required_rows: list[dict] = []
    status_counts: dict[str, int] = {}
    active_blockers = 0
    for item in items:
        status = gate_status(item, dry_run=True, env=resolved_env, spec_dir=spec_dir)
        if not status:
            continue
        preflight = _active_preflight(status, resolved_env)
        if preflight.get("blocks_active"):
            active_blockers += 1
        status_counts[status["status"]] = status_counts.get(status["status"], 0) + 1
        required_rows.append(
            {
                "target": item.get("target"),
                "task_type": item.get("task_type"),
                "lane": item.get("lane"),
                "labels": item.get("labels") or [],
                "status": status,
                "active_preflight": preflight,
            }
        )
    if not required_rows:
        recommendation = "No current backlog closer item requires runtime AC."
    elif active_blockers:
        recommendation = "Resolve missing specs or active-run environment before expecting runtime AC gates to fire."
    else:
        recommendation = (
            "Runtime AC-required backlog items are ready for active gate execution when selected."
        )
    return {
        "read_only": True,
        "items_checked": len(items),
        "required_count": len(required_rows),
        "status_counts": status_counts,
        "active_blocker_count": active_blockers,
        "env": {
            "ORCH_RUN_RUNTIME_AC": env_flag(resolved_env, "ORCH_RUN_RUNTIME_AC"),
            "ORCH_RUNTIME_AC_ALLOW_COMMANDS": env_flag(
                resolved_env, "ORCH_RUNTIME_AC_ALLOW_COMMANDS"
            ),
            "ORCH_RUNTIME_AC_TIMEOUT": env_int(resolved_env, "ORCH_RUNTIME_AC_TIMEOUT", 120),
        },
        "spec_dir": str(
            spec_dir or resolved_env.get("ORCH_RUNTIME_AC_SPEC_DIR", DEFAULT_RUNTIME_AC_SPEC_DIR)
        ),
        "required": required_rows,
        "recommendation": recommendation,
    }


def load_backlog_items(
    path: str | Path = DEFAULT_BACKLOG_JSON,
) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], str(exc)
    items = data.get("items") if isinstance(data, dict) else []
    return [item for item in items or [] if isinstance(item, dict)], None


def format_scan(summary: dict) -> str:
    lines = [
        "runtime_ac_gate scan: "
        f"required={summary['required_count']} checked={summary['items_checked']} "
        f"active_blockers={summary['active_blocker_count']}",
        f"source={summary.get('source', DEFAULT_BACKLOG_JSON)}",
        f"spec_dir={summary['spec_dir']}",
        f"recommendation: {summary['recommendation']}",
    ]
    if summary.get("load_error"):
        lines.append(f"load_error: {summary['load_error']}")
    for row in summary.get("required") or []:
        status = row["status"]
        preflight = row["active_preflight"]
        lines.append(
            f"  {row['target']}: status={status.get('status')} "
            f"active_preflight={preflight.get('status')} spec={status.get('spec_path')}"
        )
    return "\n".join(lines)


def _scan_cron_runtime_ac_gates(path: str | Path) -> tuple[dict, str | None]:
    """Extract archived runtime-AC gate events from the local orchestrator cron log."""
    log_path = Path(path)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": str(log_path),
            "exists": False,
            "event_count": 0,
            "target_count": 0,
            "status_counts": {},
            "targets": [],
        }, str(exc)

    events: list[dict] = []
    for block in re.findall(r'"runtime_ac_gates"\s*:\s*\[(.*?)\]', text, re.DOTALL):
        for match in re.finditer(
            r'\{.*?"target"\s*:\s*"([^"]+)".*?'
            r'"spec_path"\s*:\s*"([^"]+)".*?'
            r'"status"\s*:\s*"([^"]+)".*?\}',
            block,
            re.DOTALL,
        ):
            target, spec, status = match.groups()
            events.append({"target": target, "spec_path": spec, "status": status})

    status_counts: dict[str, int] = {}
    targets: dict[str, dict] = {}
    for event in events:
        status = event["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        row = targets.setdefault(
            event["target"],
            {
                "target": event["target"],
                "event_count": 0,
                "statuses": {},
                "latest_status": status,
                "latest_spec_path": event["spec_path"],
            },
        )
        row["event_count"] += 1
        row["statuses"][status] = row["statuses"].get(status, 0) + 1
        row["latest_status"] = status
        row["latest_spec_path"] = event["spec_path"]

    return {
        "path": str(log_path),
        "exists": True,
        "event_count": len(events),
        "target_count": len(targets),
        "status_counts": status_counts,
        "targets": sorted(targets.values(), key=lambda row: row["target"]),
    }, None


def _scan_db_runtime_ac_rows(db_path: str | Path) -> tuple[dict, str | None]:
    """Summarize retained runtime-AC evidence from the feedback DB."""
    path = Path(db_path)
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "runtime_ac_runs": 0,
            "production_runs": 0,
            "production_outcomes": 0,
            "evaluation_runs": 0,
            "recent_production": [],
        }, f"{path} does not exist"
    try:
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT r.run_id, r.target, r.agent, r.mode, r.source, r.experiment_id,
                   o.verifier_verdict, o.adjudicated_verdict, o.merged,
                   o.durability, o.notes
            FROM runs r
            LEFT JOIN outcomes o ON o.run_id = r.run_id
            WHERE r.task_type = 'runtime_ac'
               OR o.verifier_verdict LIKE '%RUNTIME_AC%'
               OR o.notes LIKE '%runtime%AC%'
            ORDER BY r.ts DESC
            """).fetchall()
    except sqlite3.Error as exc:
        return {
            "path": str(path),
            "exists": True,
            "runtime_ac_runs": 0,
            "production_runs": 0,
            "production_outcomes": 0,
            "evaluation_runs": 0,
            "recent_production": [],
        }, str(exc)
    finally:
        try:
            c.close()
        except Exception:
            pass

    production: list[sqlite3.Row] = []
    evaluation = 0
    for row in rows:
        target = str(row["target"] or "")
        if " [exp " in target or str(row["run_id"] or "").startswith("backfill-"):
            evaluation += 1
        else:
            production.append(row)

    recent = []
    for row in production[:10]:
        recent.append(
            {
                "run_id": row["run_id"],
                "target": row["target"],
                "agent": row["agent"],
                "mode": row["mode"],
                "source": row["source"],
                "verifier_verdict": row["verifier_verdict"],
                "adjudicated_verdict": row["adjudicated_verdict"],
                "merged": bool(row["merged"]) if row["merged"] is not None else None,
                "durability": row["durability"],
                "notes": row["notes"],
            }
        )

    return {
        "path": str(path),
        "exists": True,
        "runtime_ac_runs": len(rows),
        "production_runs": len(production),
        "production_outcomes": sum(1 for row in production if row["adjudicated_verdict"]),
        "evaluation_runs": evaluation,
        "recent_production": recent,
    }, None


def scan_history(
    *,
    log_path: str | Path = DEFAULT_CRON_LOG,
    db_path: str | Path = feedback.DB_PATH,
) -> dict:
    """Read-only history scan for runtime-AC traffic that is not in current backlog."""
    cron, cron_error = _scan_cron_runtime_ac_gates(log_path)
    db, db_error = _scan_db_runtime_ac_rows(db_path)
    archived_gate_targets = int(cron.get("target_count") or 0)
    production_rows = int(db.get("production_runs") or 0)
    if archived_gate_targets or production_rows:
        recommendation = (
            "Archived runtime-AC traffic exists; use this history for evidence/backfill review "
            "instead of waiting only for current backlog closer labels."
        )
    else:
        recommendation = (
            "No archived runtime-AC traffic was found; wait for a spec/label-backed closer or "
            "create an explicit spec-backed exercise."
        )
    return {
        "read_only": True,
        "history_available": bool(archived_gate_targets or production_rows),
        "archived_gate_targets": archived_gate_targets,
        "archived_gate_events": int(cron.get("event_count") or 0),
        "db_production_rows": production_rows,
        "db_evaluation_rows": int(db.get("evaluation_runs") or 0),
        "cron": cron,
        "db": db,
        "errors": {
            "cron": cron_error,
            "db": db_error,
        },
        "recommendation": recommendation,
    }


def format_history(summary: dict) -> str:
    lines = [
        "runtime_ac_gate history: "
        f"history_available={summary['history_available']} "
        f"archived_gate_targets={summary['archived_gate_targets']} "
        f"db_production_rows={summary['db_production_rows']} "
        f"db_evaluation_rows={summary['db_evaluation_rows']}",
        f"cron_log={summary['cron']['path']}",
        f"feedback_db={summary['db']['path']}",
        f"recommendation: {summary['recommendation']}",
    ]
    for target in (summary.get("cron", {}).get("targets") or [])[:8]:
        lines.append(
            f"  archived {target['target']}: events={target['event_count']} "
            f"latest_status={target['latest_status']} spec={target['latest_spec_path']}"
        )
    for row in (summary.get("db", {}).get("recent_production") or [])[:8]:
        lines.append(
            f"  db {row['target']}: agent={row['agent']} "
            f"verdict={row.get('adjudicated_verdict') or row.get('verifier_verdict')} "
            f"durability={row.get('durability')}"
        )
    return "\n".join(lines)


def exercise_gate() -> dict:
    """Run a real, non-mutating runtime-AC gate over a temporary command spec.

    This is an operator smoke for the active gate path when no current closer item
    requires runtime AC. It executes a harmless Python command check, refuses to
    patch an outcome by returning no run id, records its structured gate event,
    and removes the temporary spec on exit.
    """
    with tempfile.TemporaryDirectory(prefix="runtime-ac-gate-exercise-") as tmp:
        target = "stranske/Workflows#303"
        item = {
            "target": target,
            "task_type": "runtime_ac",
            "lane": "closer",
            "labels": ["runtime-ac"],
            "title": "Runtime AC gate exercise",
        }
        path = spec_path(target, spec_dir=tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        command = f"{sys.executable} -c 'print(\"ok\")'"
        path.write_text(
            json.dumps(_command_spec(str(Path(__file__).resolve().parent), command)),
            encoding="utf-8",
        )
        gate = gate_status(
            item,
            dry_run=False,
            env={
                "ORCH_RUN_RUNTIME_AC": "1",
                "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1",
                "ORCH_RUNTIME_AC_TIMEOUT": "30",
            },
            spec_dir=tmp,
            latest_run_fn=lambda target, mode=None: None,
        )
        # Exercise item is a labeled closer lane; gate_status always returns a dict here.
        assert gate is not None
        passed = (
            gate.get("status") == "executed"
            and gate.get("verdict") == "PASS"
            and not gate.get("blocks")
        )
        return {
            "exercise": "runtime_ac_gate",
            "read_only_outcomes": True,
            "writes_structured_gate_event": True,
            "mutates_repos": False,
            "temporary_spec_removed": True,
            "target": target,
            "status": "pass" if passed else "fail",
            "gate": gate,
        }


def format_exercise(report: dict) -> str:
    gate = report.get("gate") or {}
    feedback_report = gate.get("feedback") or {}
    return "\n".join(
        [
            "runtime_ac_gate exercise: "
            f"status={report.get('status')} verdict={gate.get('verdict')} "
            f"blocks={gate.get('blocks')}",
            f"target={report.get('target')}",
            f"result_count={gate.get('result_count')} pass_ratio={gate.get('pass_ratio')}",
            "feedback: "
            f"recorded={feedback_report.get('recorded')} "
            f"reason={feedback_report.get('reason', '')}",
            "mutates_repos=false temporary_spec_removed=true",
        ]
    )


def _command_spec(worktree: str, command: str) -> dict:
    return {
        "verification": {
            "id": "gate-command-runtime-ac",
            "title": "Gate command runtime AC",
            "target": "stranske/Workflows#303",
            "goal": "Verify shared runtime AC gate command execution.",
            "risk_level": "low",
        },
        "runtime_context": {"worktree": worktree},
        "acceptance_criteria": [
            {
                "id": "AC1",
                "statement": "The check prints ok.",
                "evidence_required": ["command_output"],
                "checks": [
                    {
                        "id": "AC1-CMD",
                        "type": "command",
                        "name": "Print ok",
                        "command": command,
                        "expected": "contains",
                        "contains": "ok",
                    }
                ],
            }
        ],
        "verdict_policy": {
            "require_runtime_evidence": False,
            "require_deliberate_break_for_tests": False,
            "fail_on_missing_checks": True,
            "required_check_ids": ["AC1-CMD"],
            "min_pass_ratio": 1.0,
        },
    }


def _selftest() -> None:
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="runtime-ac-gate-") as tmp:
        old_feedback_db = feedback.DB_PATH
        feedback.DB_PATH = Path(tmp) / "feedback" / "orchestrator.db"
        item_target = "stranske/Workflows#303"
        item = {
            "target": item_target,
            "task_type": "implement",
            "lane": "closer",
            "labels": ["runtime-ac"],
            "title": "Runtime-sensitive merge",
        }
        fixture_spec = runtime_ac._valid_spec()
        fixture_spec["verification"]["target"] = item_target
        fixture_spec["verification"]["repo"] = item_target.split("#", 1)[0]
        path = spec_path(item_target, spec_dir=tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixture_spec), encoding="utf-8")
        missing_path = Path(tmp) / "missing.json"
        assert "/" not in target_slug("../owner/repo#5")
        assert spec_path("o/r#1", env={"ORCH_STATE_DIR": tmp}).parent == Path(tmp) / "runtime-ac"
        assert format_post_merge_summary({"error": "db unavailable"}) == (
            "runtime-AC post-merge: unmeasured (db unavailable)"
        )
        # recheck_merged's verdict rule, on stubbed runner output. Manual criteria make the gate
        # read NEEDS_REVIEW, which must not block a PASS when every executable check passed; a
        # non-PASS executable result under that same gate, or any other gate verdict, is
        # UNRUNNABLE (incomplete). Measured 2026-10-16: three green rechecks read UNRUNNABLE
        # because the rule demanded a gate PASS no mixed spec can produce.
        # The stubbed runner needs no dependency installation or network access.
        (Path(tmp) / ".venv").symlink_to(sys.prefix, target_is_directory=True)
        saved_run_verification = runtime_ac.run_verification

        def stub(payload):
            runtime_ac.run_verification = lambda spec, **kw: payload
            return recheck_merged(fixture_spec, tmp)

        try:
            manual = {"type": "manual", "status": "NEEDS_REVIEW"}
            green = {"type": "command", "status": "PASS", "returncode": 0}
            for gate_verdict in ("PASS", "NEEDS_REVIEW"):
                verdict = stub(
                    {"gate": {"verdict": gate_verdict}, "check_results": [green, manual]}
                )
                assert verdict["result"] == "PASS" and verdict["reason"] is None, verdict
            for status in ("SKIP", "ERROR"):
                verdict = stub(
                    {
                        "gate": {"verdict": "NEEDS_REVIEW"},
                        "check_results": [green, {"type": "command", "status": status}, manual],
                    }
                )
                assert verdict["result"] == "UNRUNNABLE", verdict
                assert verdict["reason"] == "incomplete", verdict
            verdict = stub({"gate": {"verdict": "FAIL"}, "check_results": [green]})
            assert (verdict["result"], verdict["reason"]) == ("UNRUNNABLE", "incomplete"), verdict
            verdict = stub({"gate": {"verdict": "NEEDS_REVIEW"}, "check_results": [manual]})
            assert (verdict["result"], verdict["reason"]) == ("UNRUNNABLE", "manual_only"), verdict
        finally:
            runtime_ac.run_verification = saved_run_verification
        assert required({"labels": ["Type: Runtime-AC"]}, missing_path) is True
        assert required({"labels": ["AC-CHECKS"]}, missing_path) is True
        with tempfile.TemporaryDirectory(prefix="runtime-ac-env-") as env_tmp:
            env_path = spec_path(item_target, env={"ORCH_RUNTIME_AC_SPEC_DIR": env_tmp})
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(json.dumps(fixture_spec), encoding="utf-8")
            env_planned = gate_status(item, dry_run=True, env={"ORCH_RUNTIME_AC_SPEC_DIR": env_tmp})
            assert env_planned is not None
            assert env_planned["spec_path"] == str(env_path), env_planned

        planned = gate_status(item, dry_run=True, spec_dir=tmp)
        assert planned is not None
        assert planned["status"] == "planned" and planned["blocks"] is False, planned
        assert (
            gate_status(
                {**item, "target": "stranske/Workflows#999", "labels": []},
                dry_run=False,
                spec_dir=tmp,
            )
            is None
        )
        assert gate_status({**item, "lane": "opener"}, dry_run=True, spec_dir=tmp) is None
        missing = gate_status(
            {**item, "target": "stranske/Workflows#304"},
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
        )
        assert missing is not None
        assert missing["status"] == "missing_spec" and missing["blocks"] is True, missing
        disabled = gate_status(item, dry_run=False, env={}, spec_dir=tmp)
        assert disabled is not None
        assert disabled["status"] == "required_but_not_run" and disabled["blocks"] is True, disabled
        path.write_text("{not json", encoding="utf-8")
        malformed = gate_status(item, dry_run=False, env={"ORCH_RUN_RUNTIME_AC": "1"}, spec_dir=tmp)
        assert malformed is not None
        assert malformed["status"] == "failed" and malformed["blocks"] is True, malformed

        recorded = []
        path.write_text(json.dumps(fixture_spec), encoding="utf-8")

        def fake_pass(spec, **kwargs):
            assert kwargs["confirm_run"] is True, kwargs
            return {
                "gate": {
                    "verification_id": "fixture",
                    "verdict": "PASS",
                    "verifier_verdict": "PASS_RUNTIME_AC",
                    "pass_ratio": 1.0,
                    "result_count": 1,
                    "blocking": [],
                    "needs_review": [],
                }
            }

        def fake_fail(spec, **kwargs):
            return {
                "gate": {
                    "verification_id": "fixture",
                    "verdict": "FAIL",
                    "verifier_verdict": "FAIL_RUNTIME_AC",
                    "pass_ratio": 0.0,
                    "result_count": 1,
                    "blocking": [{"check_id": "AC1", "status": "FAIL"}],
                    "needs_review": [],
                }
            }

        def fake_needs_review(spec, **kwargs):
            return {
                "gate": {
                    "verification_id": "fixture-review",
                    "verdict": "NEEDS_REVIEW",
                    "verifier_verdict": "NEEDS_REVIEW_RUNTIME_AC",
                    "pass_ratio": 0.5,
                    "result_count": 1,
                    "blocking": [],
                    "needs_review": [{"check_id": "AC1", "status": "NEEDS_REVIEW"}],
                }
            }

        def fake_record(run_id, gate):
            recorded.append((run_id, gate["verdict"]))
            return {"recorded": True}

        passed = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_TIMEOUT": "15"},
            spec_dir=tmp,
            run_fn=fake_pass,
            latest_run_fn=lambda target, mode=None: "remote-run-303",
            record_fn=fake_record,
        )
        assert passed is not None
        assert passed["status"] == "executed" and passed["blocks"] is False, passed
        assert passed["run_id"] == "remote-run-303" and recorded == [
            ("remote-run-303", "PASS")
        ], passed
        feedback_error = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_pass,
            latest_run_fn=lambda target, mode=None: (_ for _ in ()).throw(RuntimeError("db busy")),
        )
        assert feedback_error is not None
        assert (
            feedback_error["status"] == "executed" and feedback_error["blocks"] is False
        ), feedback_error
        assert feedback_error["feedback"]["recorded"] is False, feedback_error
        failed = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_fail,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert failed is not None
        assert failed["status"] == "executed" and failed["blocks"] is True, failed
        assert failed["feedback"]["recorded"] is False, failed
        needs_review = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_needs_review,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert needs_review is not None
        assert (
            needs_review["verdict"] == "NEEDS_REVIEW" and needs_review["blocks"] is True
        ), needs_review

        structured = feedback.runtime_ac_gate_events(limit=100)
        statuses = {row.get("gate_status") for row in structured}
        verdicts = {row.get("verifier_verdict") for row in structured}
        assert {
            "required",
            "planned",
            "missing_spec",
            "skipped",
            "error",
            "executed",
        } <= statuses, structured
        assert {"PASS", "NEEDS_REVIEW", "FAIL"} <= verdicts, structured
        assert all(row.get("spec_path_matches_target") is True for row in structured), structured

        materialized_target = "stranske/Pension-Data#703"
        materialized_spec = json.loads(json.dumps(fixture_spec))
        materialized_spec["verification"]["target"] = materialized_target
        materialized_spec["verification"]["repo"] = "stranske/Pension-Data"
        materialized = materialize_range_spec(
            materialized_target,
            materialized_spec,
            spec_dir=tmp,
            producer_run_id="range-run-703",
        )
        assert materialized["status"] == "materialized", materialized
        materialized_item = {
            "target": materialized_target,
            "task_type": "implement",
            "lane": "closer",
            "labels": [],
        }
        next_gate = gate_status(
            materialized_item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
            run_fn=fake_pass,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert next_gate is not None
        assert next_gate["spec_path"] == materialized["spec_path"], next_gate
        assert next_gate["spec_hash"] == materialized["spec_hash"], (next_gate, materialized)
        wrong_target = materialize_range_spec(
            "stranske/Pension-Data#704",
            fixture_spec,
            spec_dir=tmp,
        )
        assert wrong_target["status"] == "materialization_failed", wrong_target
        assert "spec_target_mismatch" in wrong_target["terminal_reason"], wrong_target
        assert not spec_path("stranske/Pension-Data#704", spec_dir=tmp).exists()

        command = f"{sys.executable} -c 'print(\"ok\")'"
        path.write_text(
            json.dumps(_command_spec(str(Path(__file__).resolve().parent), command)),
            encoding="utf-8",
        )
        integrated = gate_status(
            item,
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1"},
            spec_dir=tmp,
            latest_run_fn=lambda target, mode=None: None,
        )
        assert integrated is not None
        assert integrated["status"] == "executed" and integrated["verdict"] == "PASS", integrated
        assert integrated["blocks"] is False, integrated

        scan_item = {
            "target": item_target,
            "task_type": "implement",
            "lane": "closer",
            "labels": ["runtime-ac"],
        }
        ignored = {
            "target": "stranske/Workflows#305",
            "task_type": "implement",
            "lane": "opener",
            "labels": ["runtime-ac"],
        }
        scan = scan_items([scan_item, ignored], env={}, spec_dir=tmp)
        assert scan["required_count"] == 1 and scan["items_checked"] == 2, scan
        assert scan["status_counts"]["planned"] == 1, scan
        assert scan["required"][0]["active_preflight"]["status"] == "requires_env", scan
        scan_ready = scan_items([scan_item], env={"ORCH_RUN_RUNTIME_AC": "1"}, spec_dir=tmp)
        assert (
            scan_ready["required"][0]["active_preflight"]["status"] == "ready_to_execute"
        ), scan_ready
        missing_scan = scan_items(
            [{**scan_item, "target": "stranske/Workflows#306"}],
            env={"ORCH_RUN_RUNTIME_AC": "1"},
            spec_dir=tmp,
        )
        assert missing_scan["status_counts"]["missing_spec"] == 1, missing_scan
        assert missing_scan["active_blocker_count"] == 1, missing_scan
        malformed_backlog = Path(tmp) / "bad-backlog.json"
        malformed_backlog.write_text("{not json", encoding="utf-8")
        loaded_items, load_error = load_backlog_items(malformed_backlog)
        assert loaded_items == [] and load_error, (loaded_items, load_error)
        missing_items, missing_error = load_backlog_items(Path(tmp) / "missing.json")
        assert missing_items == [] and missing_error, (missing_items, missing_error)

        history_log = Path(tmp) / "cron.log"
        history_log.write_text(
            """
            {
              "runtime_ac_gates": [
                {
                  "target": "stranske/Workflows#2479",
                  "spec_path": "/tmp/stranske__Workflows__2479.json",
                  "status": "missing_spec",
                  "blocks": true
                }
              ]
            }
            {
              "runtime_ac_gates": []
            }
            """,
            encoding="utf-8",
        )
        history = scan_history(log_path=history_log, db_path=Path(tmp) / "missing.db")
        assert history["history_available"] is True, history
        assert history["archived_gate_targets"] == 1, history
        assert history["cron"]["targets"][0]["target"] == "stranske/Workflows#2479"
        assert "runtime_ac_gate history:" in format_history(history), history

        exercise = exercise_gate()
        assert exercise["status"] == "pass", exercise
        assert exercise["mutates_repos"] is False, exercise
        assert exercise["gate"]["feedback"]["recorded"] is False, exercise
        assert "runtime_ac_gate exercise:" in format_exercise(exercise), exercise

        feedback.DB_PATH = old_feedback_db

    print(
        "runtime_ac_gate.py selftest: OK (requirement detection, active gate, "
        "feedback patch, command integration, backlog scan, history scan)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Runtime AC gate helpers.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--scan-backlog",
        nargs="?",
        const=str(DEFAULT_BACKLOG_JSON),
        help="read-only scan of backlog.json for runtime-AC-required closer items",
    )
    parser.add_argument(
        "--exercise",
        action="store_true",
        help="run a non-mutating active runtime-AC gate exercise with a temporary command spec",
    )
    parser.add_argument(
        "--scan-history",
        action="store_true",
        help="read-only scan of local archived runtime-AC gate and feedback evidence",
    )
    parser.add_argument(
        "--materialize-range-spec",
        type=Path,
        help="validate and atomically install a range-lane runtime-AC JSON artifact",
    )
    parser.add_argument(
        "--target",
        help="exact owner/repo#number target required with --materialize-range-spec",
    )
    parser.add_argument(
        "--producer-run-id",
        help="optional range-lane producer run id retained with materialization output",
    )
    parser.add_argument(
        "--cron-log",
        type=Path,
        default=DEFAULT_CRON_LOG,
        help="cron log to scan with --scan-history",
    )
    parser.add_argument(
        "--feedback-db",
        type=Path,
        default=feedback.DB_PATH,
        help="feedback DB to scan with --scan-history",
    )
    parser.add_argument("--spec-dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if args.materialize_range_spec:
        if not args.target:
            parser.error("--materialize-range-spec requires --target")
        report = materialize_range_spec(
            args.target,
            args.materialize_range_spec,
            spec_dir=args.spec_dir,
            producer_run_id=args.producer_run_id,
        )
        print(json.dumps(report, indent=2) if args.as_json else json.dumps(report, indent=2))
        return 0 if report.get("status") == "materialized" else 1
    if args.exercise:
        report = exercise_gate()
        print(json.dumps(report, indent=2) if args.as_json else format_exercise(report))
        return 0 if report.get("status") == "pass" else 1
    if args.scan_history:
        summary = scan_history(log_path=args.cron_log, db_path=args.feedback_db)
        print(json.dumps(summary, indent=2) if args.as_json else format_history(summary))
        return 0
    if args.scan_backlog:
        items, load_error = load_backlog_items(args.scan_backlog)
        summary = scan_items(items, spec_dir=args.spec_dir)
        summary["source"] = str(args.scan_backlog)
        if load_error:
            summary["load_error"] = load_error
            summary["recommendation"] = (
                "Backlog could not be loaded; refresh the handoff backlog snapshot before expecting gates to fire."
            )
        print(json.dumps(summary, indent=2) if args.as_json else format_scan(summary))
        return 0
    parser.print_usage()
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
