#!/usr/bin/env python3
"""Resumable, non-publishing promotion lifecycle for verified experiment syntheses.

The canonical capability lifecycle remains owned by :mod:`capabilities`.  This
module adds only a subordinate ``delivery_phase`` and derives its canonical
state from a fixed mapping.  It creates local candidate artifacts; it never
pushes, opens an issue/PR, merges, or becomes a second delivery controller.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import capabilities
import feedback
import local_verify
import runtime_ac

SCHEMA_VERSION = 1
STATE_NAME = "synthesis-promotion.json"
LOCK_NAME = ".synthesis-promotion.lock"
CANDIDATE_JSON = "synthesis-delivery-candidate.json"
CANDIDATE_BODY = "synthesis-delivery-candidate.md"
DEFAULT_TTL_DAYS = 14
DEFAULT_CANDIDATE_TTL_DAYS = 7
DEFAULT_MAX_RETRIES = 3

# Subordinate phases only. These are not a second capability-lifecycle enum.
DELIVERY_PHASES = (
    "evaluated",
    "synth_running",
    "synth_complete",
    "synth_verified",
    "candidate_ready",
    "delegated_or_pr",
    "merged",
    "durable",
    "discarded",
)
PHASE_TO_CANONICAL = {
    "evaluated": "generated",
    "synth_running": "generated",
    "synth_complete": "generated",
    "synth_verified": "validated",
    "candidate_ready": "wired",
    "delegated_or_pr": "exercised",
    "merged": "canary",
    "durable": "active",
    "discarded": "retired",
}
PROMOTION_TRANSITIONS = {
    "evaluated": {"synth_running", "discarded"},
    "synth_running": {"synth_complete", "discarded"},
    "synth_complete": {"synth_verified", "discarded"},
    "synth_verified": {"candidate_ready", "discarded"},
    "candidate_ready": {"delegated_or_pr", "discarded"},
    "delegated_or_pr": {"merged", "discarded"},
    "merged": {"durable", "discarded"},
    "durable": set(),
    "discarded": set(),
}
TERMINAL_PHASES = {"durable", "discarded"}
FAILURE_DURABILITY = {"reverted", "reworked", "reopened", "broke_later"}
SECRET_RE = re.compile(
    r"(?:bearer\s+[a-z0-9._~+/-]{12,}|(?:gh[opurs]_|sk-|api[_-]?key\s*[:=])[a-z0-9._-]{8,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
SENSITIVE_PATH_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\.|$)|id_rsa$|id_ed25519$|[^/]+\.(?:pem|p12|key)$)",
    re.IGNORECASE,
)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def state_path(exp_dir: str | Path) -> Path:
    return Path(exp_dir) / STATE_NAME


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _locked(exp_dir: str | Path) -> Iterator[None]:
    root = Path(exp_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state(exp_dir: str | Path) -> dict | None:
    path = state_path(exp_dir)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_state(payload)
    return payload


def validate_state(state: dict) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported synthesis-promotion schema")
    phase = state.get("delivery_phase")
    if phase not in DELIVERY_PHASES:
        raise ValueError(f"invalid synthesis delivery phase={phase!r}")
    canonical = state.get("canonical_state")
    if canonical != PHASE_TO_CANONICAL[phase]:
        raise ValueError("synthesis delivery phase/canonical state mismatch")
    if canonical not in capabilities.CANONICAL_STATES:
        raise ValueError("synthesis maps outside canonical capability states")
    if state.get("publication", {}).get("direct_publication_allowed") is not False:
        raise ValueError("synthesis promotion must prohibit direct publication")
    history = state.get("phase_history") or []
    ids = [row.get("event_id") for row in history]
    if any(not event_id for event_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("synthesis phase history is not exactly-once")


def _accepted_source_influences(source_run_ids: list[str]) -> list[dict]:
    """Read bounded accepted role/skill/workflow/capability lineage from the Brain."""
    if not source_run_ids or not Path(feedback.DB_PATH).exists():
        return []
    conn = sqlite3.connect(str(feedback.DB_PATH))
    conn.row_factory = sqlite3.Row
    found: dict[tuple[str, str, str], dict] = {}
    try:
        placeholders = ",".join("?" for _ in source_run_ids)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "influence_edges" in tables:
            for row in conn.execute(
                f"SELECT target_run_id,influence_type,influence_id,acceptance_gate_id "
                f"FROM influence_edges WHERE accepted=1 AND target_run_id IN ({placeholders})",
                source_run_ids,
            ):
                key = (str(row[0]), str(row[1]), str(row[2]))
                found[key] = {
                    "target_run_id": row[0],
                    "influence_type": row[1],
                    "influence_id": row[2],
                    "acceptance_gate_id": row[3],
                }
        if "completion_events" in tables:
            for row in conn.execute(
                f"SELECT run_id,payload_json FROM completion_events "
                f"WHERE run_id IN ({placeholders}) AND validation_status='accepted'",
                source_run_ids,
            ):
                try:
                    payload = json.loads(row[1])
                except (TypeError, json.JSONDecodeError):
                    continue
                for field, kind in (
                    ("capability_ids", "capability"),
                    ("role_ids", "role"),
                    ("skill_ids", "skill"),
                    ("workflow_ids", "workflow"),
                ):
                    for influence_id in payload.get(field) or []:
                        key = (str(row[0]), kind, str(influence_id))
                        found.setdefault(
                            key,
                            {
                                "target_run_id": row[0],
                                "influence_type": kind,
                                "influence_id": influence_id,
                                "acceptance_gate_id": None,
                            },
                        )
    finally:
        conn.close()
    return [found[key] for key in sorted(found)][:100]


def _source_lineage(meta: dict, exp_id: str, evaluator_ids: list[str]) -> dict:
    raw_members = list(meta.get("members") or [])
    if raw_members:
        members = [
            {
                "arm_id": row.get("arm_id"),
                "member_id": row.get("member_id"),
                "agent": row.get("agent"),
                "profile_id": row.get("profile_id"),
                "strategy": row.get("strategy") or "single",
                "source_run_id": f"{exp_id}:member:{row.get('member_id')}",
            }
            for row in raw_members
        ]
    else:
        members = [
            {
                "arm_id": None,
                "member_id": agent,
                "agent": agent,
                "profile_id": None,
                "strategy": "legacy_agent",
                "source_run_id": f"{exp_id}:{agent}",
            }
            for agent in meta.get("agents") or []
        ]
    source_run_ids = [str(row["source_run_id"]) for row in members]
    return {
        "experiment_id": exp_id,
        "repo": meta.get("repo"),
        "base": meta.get("base"),
        "base_sha": meta.get("base_sha"),
        "task_type": meta.get("task_type") or "implement",
        "arms": meta.get("arms") or [],
        "members": members,
        "profile_ids": sorted(
            {str(row.get("profile_id")) for row in members if row.get("profile_id")}
        ),
        "evaluator_ids": evaluator_ids,
        "accepted_influences": _accepted_source_influences(source_run_ids),
        "shared_capacity_pool_id": meta.get("capacity_pool_id") or "experiment-shared-pool",
        "shared_capacity_cost": meta.get("shared_capacity_cost"),
    }


def ensure_evaluated_state(
    exp_dir: str | Path,
    *,
    meta: dict | None = None,
    now: int | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    candidate_ttl_days: int = DEFAULT_CANDIDATE_TTL_DAYS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    root = Path(exp_dir)
    ts = int(now or time.time())
    with _locked(root):
        existing = load_state(root)
        if existing is not None:
            return existing
        meta = dict(meta or json.loads((root / "meta.json").read_text(encoding="utf-8")))
        exp_id = str(meta.get("exp_id") or root.name)
        maps_path = root / "eval-maps.json"
        evaluator_ids = []
        if maps_path.exists():
            maps = json.loads(maps_path.read_text(encoding="utf-8"))
            evaluator_ids = sorted(str(value) for value in maps) if isinstance(maps, dict) else []
        initial_event = (
            "promotion:" + hashlib.sha256(f"{exp_id}|evaluated".encode()).hexdigest()[:24]
        )
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": exp_id,
            "repo": meta.get("repo"),
            "delivery_phase": "evaluated",
            "canonical_state": PHASE_TO_CANONICAL["evaluated"],
            "created_ts": ts,
            "updated_ts": ts,
            "expires_ts": ts + max(1, int(ttl_days)) * 86400,
            "candidate_expires_ts": None,
            "max_retries": max(1, int(max_retries)),
            "retry": {"count": 0, "next_retry_ts": None, "failures": []},
            "phase_history": [
                {
                    "event_id": initial_event,
                    "from": None,
                    "to": "evaluated",
                    "canonical_state": PHASE_TO_CANONICAL["evaluated"],
                    "reason": "cross_evaluation_complete",
                    "ts": ts,
                }
            ],
            "lineage": _source_lineage(meta, exp_id, evaluator_ids),
            "synthesis": {},
            "verification": {},
            "candidate": {},
            "delivery": {},
            "outcome": {},
            "rollback": {
                "available": True,
                "action": "retire local candidate and preserve evidence; no remote mutation",
            },
            "publication": {
                "direct_publication_allowed": False,
                "auto_merge_allowed": False,
                "delivery_controller": "Workflows auto-pilot/Keepalive",
                "requires_external_delivery_link": True,
            },
            "candidate_ttl_days": max(1, int(candidate_ttl_days)),
            "mirrored_events": [],
        }
        validate_state(state)
        _atomic_json(state_path(root), state)
        return state


def transition(
    state: dict,
    target_phase: str,
    *,
    reason: str,
    evidence: dict | None = None,
    now: int | None = None,
) -> tuple[dict, bool]:
    validate_state(state)
    current = state["delivery_phase"]
    if current == target_phase:
        return state, False
    if target_phase not in PROMOTION_TRANSITIONS[current]:
        raise ValueError(f"invalid synthesis promotion transition {current}->{target_phase}")
    # Independent fail-closed invariant behind the deliberate-break test.
    if target_phase == "candidate_ready" and current != "synth_verified":
        raise ValueError("candidate_ready requires synth_verified predecessor")
    canonical = PHASE_TO_CANONICAL[target_phase]
    if canonical not in capabilities.CANONICAL_STATES:
        raise ValueError("promotion target does not map to a canonical state")
    ts = int(now or time.time())
    event_id = (
        "promotion:"
        + hashlib.sha256(
            f"{state['experiment_id']}|{current}|{target_phase}|{reason}".encode()
        ).hexdigest()[:24]
    )
    if any(row.get("event_id") == event_id for row in state.get("phase_history") or []):
        return state, False
    state = json.loads(json.dumps(state))
    state["delivery_phase"] = target_phase
    state["canonical_state"] = canonical
    state["updated_ts"] = ts
    state.setdefault("phase_history", []).append(
        {
            "event_id": event_id,
            "from": current,
            "to": target_phase,
            "canonical_state": canonical,
            "reason": str(reason)[:256],
            "evidence_hash": _hash(evidence) if evidence else None,
            "ts": ts,
        }
    )
    if target_phase == "candidate_ready":
        state["candidate_expires_ts"] = ts + int(state["candidate_ttl_days"]) * 86400
    if target_phase == "discarded":
        state["rollback"] = {
            **state.get("rollback", {}),
            "retired_ts": ts,
            "reason": str(reason)[:256],
            "executed": True,
            "remote_mutation": False,
        }
    validate_state(state)
    return state, True


def _record_retry(state: dict, reason: str, now: int) -> dict:
    state = json.loads(json.dumps(state))
    retry = state.setdefault("retry", {"count": 0, "failures": []})
    retry["count"] = int(retry.get("count") or 0) + 1
    retry.setdefault("failures", []).append(
        {"attempt": retry["count"], "reason": str(reason)[:256], "ts": now}
    )
    retry["next_retry_ts"] = now + min(6 * 3600, (2 ** (retry["count"] - 1)) * 300)
    state["updated_ts"] = now
    return state


def _git(worktree: Path, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def default_completion_probe(state: dict) -> dict:
    synthesis = state.get("synthesis") or {}
    run_id = synthesis.get("current_run_id") or synthesis.get("run_id")
    log = Path(str(synthesis.get("log") or ""))
    marker = log.parent / "done" / f"{run_id}.json" if run_id and str(log) else None
    if marker and marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            rc = int(payload.get("rc"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {"status": "interrupted", "reason": f"invalid completion marker: {exc}"}
        if rc != 0:
            return {"status": "interrupted", "reason": f"synthesis marker rc={rc}", "rc": rc}
        worktree = Path(str(synthesis.get("worktree") or ""))
        head = _git(worktree, ["rev-parse", "HEAD"])
        if head.returncode != 0:
            return {"status": "interrupted", "reason": "synthesis worktree HEAD unreadable"}
        current_head = head.stdout.strip()
        if current_head == synthesis.get("launch_head"):
            return {"status": "failed", "reason": "synthesis completed without a new commit"}
        return {
            "status": "complete",
            "commit": current_head,
            "marker_hash": _hash(payload),
        }
    pid = synthesis.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            return {"status": "pending"}
        except (OSError, ValueError):
            pass
    return {"status": "interrupted", "reason": "synthesis exited without completion marker"}


def _safe_command(argv: list[str]) -> bool:
    if not argv or any(not isinstance(token, str) or not token for token in argv):
        return False
    if argv[:3] == ["git", "diff", "--check"]:
        return True
    if argv[0] == "pytest":
        return True
    if argv[:3] == ["python3", "-m", "pytest"]:
        return True
    if Path(argv[0]).name in {"python", "python3"} and len(argv) >= 3:
        return argv[1] == "-m" and argv[2] in {"pytest", "unittest"}
    if argv[:3] == ["uv", "run", "pytest"]:
        return True
    if argv[:2] in (["npm", "test"], ["pnpm", "test"], ["cargo", "test"]):
        return True
    if len(argv) >= 3 and argv[:2] in (["npm", "run"], ["pnpm", "run"]):
        return argv[2].startswith("test") or argv[2] in {"lint", "typecheck"}
    return False


def _run_gate(argv: list[str], worktree: Path, timeout: int = 600) -> dict:
    if not _safe_command(argv):
        return {"ok": False, "reason": "repo gate command is not allowlisted", "argv": argv}
    started = time.time()
    try:
        result = subprocess.run(
            argv,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "argv": argv,
            "duration_s": round(time.time() - started, 3),
            "output_hash": _hash(
                {"stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "argv": argv,
            "duration_s": round(time.time() - started, 3),
            "reason": "timeout",
            "transient": True,
        }


def _verification_config(exp_dir: Path, meta: dict) -> dict:
    path = exp_dir / "promotion-verification.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("promotion-verification.json must be an object")
        return payload
    return dict(meta.get("promotion_verification") or {})


def verify_synthesis(state: dict, exp_dir: str | Path) -> dict:
    root = Path(exp_dir)
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    config = _verification_config(root, meta)
    worktree = Path(str((state.get("synthesis") or {}).get("worktree") or ""))
    base_ref = str(config.get("base_ref") or meta.get("base_sha") or meta.get("base") or "HEAD^")
    changed = _git(worktree, ["diff", "--name-only", f"{base_ref}...HEAD"])
    changed_paths = (
        sorted(path for path in changed.stdout.splitlines() if path.strip())
        if changed.returncode == 0
        else []
    )
    safe_paths = bool(changed_paths) and len(changed_paths) <= int(
        config.get("max_changed_paths", 200)
    )
    safe_paths = safe_paths and all(
        not Path(path).is_absolute()
        and ".." not in Path(path).parts
        and not path.startswith(".git/")
        for path in changed_paths
    )
    allowed_prefixes = [
        str(value).rstrip("/") for value in config.get("allowed_path_prefixes") or []
    ]
    if allowed_prefixes:
        safe_paths = safe_paths and all(
            any(path == prefix or path.startswith(prefix + "/") for prefix in allowed_prefixes)
            for path in changed_paths
        )

    diff = _git(worktree, ["diff", "--no-ext-diff", "--unified=0", f"{base_ref}...HEAD"])
    diff_text = diff.stdout if diff.returncode == 0 else ""
    secret_hits = []
    if SECRET_RE.search(diff_text):
        secret_hits.append("diff_content_secret_pattern")
    secret_hits.extend(
        f"sensitive_path:{path}" for path in changed_paths if SENSITIVE_PATH_RE.search(path)
    )

    spec_text = (root / "spec.md").read_text(encoding="utf-8", errors="replace")
    deliberate_required = bool(re.search(r"deliberate[- _]break", spec_text, re.IGNORECASE))
    local_cfg = config.get("local_verify") or {}
    if local_cfg:
        local_command = str(local_cfg.get("test_cmd") or "")
        try:
            local_argv = shlex.split(local_command)
        except ValueError:
            local_argv = []
        if not _safe_command(local_argv):
            local_result = {
                "verdict": "ERROR",
                "ok": False,
                "reason": "local_verify test command is not allowlisted",
            }
            local_ok = False
        else:
            local_result = local_verify.verify(
                worktree,
                base_ref=str(local_cfg.get("base_ref") or base_ref),
                test_cmd=local_command,
                test_paths=list(local_cfg.get("test_paths") or []),
                timeout=int(local_cfg.get("timeout") or 180),
            )
            local_ok = local_result.get("verdict") == "PASS"
    elif deliberate_required:
        local_result = {
            "verdict": "MISSING",
            "ok": False,
            "reason": "spec requires deliberate-break evidence but no local_verify plan exists",
        }
        local_ok = False
    else:
        local_result = {"verdict": "NOT_APPLICABLE", "ok": True}
        local_ok = True

    runtime_cfg = config.get("runtime_ac") or {}
    if runtime_cfg:
        runtime_path = Path(str(runtime_cfg.get("spec_path") or ""))
        if not runtime_path.is_absolute():
            runtime_path = root / runtime_path
        try:
            runtime_spec = runtime_ac.parse_spec_json(runtime_path.read_text(encoding="utf-8"))
            runtime_result = runtime_ac.run_verification(
                runtime_spec,
                confirm_run=True,
                allow_command_checks=bool(runtime_cfg.get("allow_command_checks", False)),
                timeout=int(runtime_cfg.get("timeout") or 120),
            )
            runtime_ok = (runtime_result.get("gate") or {}).get("verdict") == "PASS"
        except Exception as exc:
            runtime_result = {"gate": {"verdict": "ERROR"}, "reason": str(exc)[:256]}
            runtime_ok = False
    else:
        runtime_result = {"gate": {"verdict": "NOT_APPLICABLE"}}
        runtime_ok = True

    gate_specs = list(config.get("repo_gates") or [])
    repo_gates = []
    if gate_specs:
        for raw in gate_specs:
            argv = list(raw) if isinstance(raw, list) else shlex.split(str(raw))
            repo_gates.append(
                _run_gate(argv, worktree, int(config.get("repo_gate_timeout") or 600))
            )
    else:
        repo_gates.append(
            _run_gate(["git", "diff", "--check", f"{base_ref}...HEAD"], worktree, 120)
        )
    repo_ok = all(row.get("ok") for row in repo_gates)
    scope_ok = changed.returncode == 0 and safe_paths
    secrets_ok = diff.returncode == 0 and not secret_hits
    passed = scope_ok and secrets_ok and local_ok and runtime_ok and repo_ok
    transient = any(row.get("transient") for row in repo_gates) or (
        (runtime_result.get("gate") or {}).get("verdict") == "ERROR"
    )
    # Narrowed rather than coerced: an older local_verify has no such key, and a malformed one
    # must not put a non-list into the evidence record the promotion hash is taken over.
    _hollow = local_result.get("hollow_nodes")
    local_hollow = [str(node) for node in _hollow] if isinstance(_hollow, list) else []
    evidence = {
        "scope": {
            "ok": scope_ok,
            "base_ref": base_ref,
            "changed_paths": changed_paths,
            "changed_paths_hash": _hash(changed_paths),
        },
        "secret_scan": {"ok": secrets_ok, "finding_ids": secret_hits},
        "local_verify": {
            "ok": local_ok,
            "verdict": local_result.get("verdict"),
            # `verdict` grades the whole test COMMAND, so it says PASS as soon as ONE candidate
            # test fails against the base. These two grade it per test NODE, so a promotion whose
            # deliberate-break proof rests on one real test beside two tautologies is
            # distinguishable in the evidence from a clean one. Advisory: `local_ok` and the
            # promotion gates are unchanged.
            "node_verdict": local_result.get("node_verdict"),
            "hollow_nodes": local_hollow,
            "result_hash": _hash(local_result),
            "test_paths": local_result.get("test_paths") or local_cfg.get("test_paths") or [],
            "test_cmd": local_cfg.get("test_cmd"),
        },
        "runtime_ac": {
            "ok": runtime_ok,
            "verdict": (runtime_result.get("gate") or {}).get("verdict"),
            "result_hash": _hash(runtime_result),
            "spec_path": str(runtime_cfg.get("spec_path") or "") or None,
        },
        "repo_gates": repo_gates,
        "deliberate_break_status": (
            "PASS"
            if deliberate_required and local_ok
            else "FAIL" if deliberate_required else "NOT_REQUIRED"
        ),
    }
    return {
        "passed": passed,
        "transient": bool(transient),
        "evidence": evidence,
        "evidence_hash": _hash(evidence),
        "failure_reason": None if passed else "synthesis verification gates did not all pass",
    }


def _break_caveat(local_gate: dict) -> str:
    """Qualify the candidate body's deliberate-break claim from the per-node evidence.

    Empty string on a clean per-node proof, and on evidence that predates per-node grading (no
    `node_verdict` key), so an older record reads exactly as it did before.
    """
    hollow = list(local_gate.get("hollow_nodes") or [])
    node_verdict = local_gate.get("node_verdict")
    if hollow:
        return (
            f" NOT a clean per-node proof: {len(hollow)} candidate test node(s) PASS against the "
            f"base implementation and are therefore no part of the deliberate-break evidence — "
            f"{local_verify.name_nodes(hollow)}. Treat what those cover as unproven when reviewing "
            f"the diff."
        )
    if node_verdict is not None and node_verdict != "PASS":
        return (
            f" The deliberate-break proof was NOT attributed per test node ({node_verdict}), so "
            f"tautologies sharing a file with a real test cannot be ruled out from this evidence."
        )
    return ""


def _candidate_body(state: dict, exp_dir: Path) -> str:
    lineage = state["lineage"]
    verification = state["verification"]
    changed = ((verification.get("evidence") or {}).get("scope") or {}).get("changed_paths") or []
    named_paths = changed[:20] or ["the synthesized worktree diff"]
    tasks = "\n".join(
        f"- [ ] Deliver the verified synthesis change affecting `{path}` through the repository's normal issue/PR workflow."
        for path in named_paths
    )
    local_gate = (verification.get("evidence") or {}).get("local_verify") or {}
    repo_gates = (verification.get("evidence") or {}).get("repo_gates") or []
    named_gate = local_gate.get("test_cmd") or (
        shlex.join(repo_gates[0].get("argv") or []) if repo_gates else "git diff --check"
    )
    # The Why paragraph below CLAIMS the deliberate-break gate passed, and that claim is graded per
    # test COMMAND: it holds as soon as one candidate test fails against the base. Where the
    # per-node pass says otherwise, the claim gets its qualifier in the same sentence -- a reader
    # of this candidate is the last party positioned to notice, and the body is what carries the
    # claim forward. Stated, never a Task: making an unproven node BLOCK delivery is a gating
    # decision, and this is the reporting half of it.
    break_caveat = _break_caveat(local_gate)
    lineage_summary = {
        "experiment_id": lineage.get("experiment_id"),
        "source_arm_ids": [row.get("arm_id") for row in lineage.get("members") or []],
        "source_member_ids": [row.get("member_id") for row in lineage.get("members") or []],
        "profile_ids": lineage.get("profile_ids") or [],
        "evaluator_ids": lineage.get("evaluator_ids") or [],
        "synthesis_run_ids": (state.get("synthesis") or {}).get("run_ids") or [],
        "shared_capacity_pool_id": lineage.get("shared_capacity_pool_id"),
        "verification_evidence_hash": verification.get("evidence_hash"),
    }
    return f"""## Why

Experiment `{lineage.get('experiment_id')}` produced a locally committed synthesis whose source specification is `{exp_dir / 'spec.md'}`. The synthesis passed the recorded scope, secret, local/deliberate-break, runtime-AC, and repository gates before this candidate was created; this is a verified delivery opportunity, not an evaluator-score promotion.{break_caveat}

## Scope

- Deliver only commit `{(state.get('synthesis') or {}).get('commit')}` and the paths listed below.
- Preserve experiment, arm, member, evaluator, synthesis, profile, and shared-capacity lineage.
- Route the work through the repository's existing issue/PR and Workflows Keepalive ownership.

## Non-Goals

- Do not publish or merge directly from the experiment worktree.
- Do not broaden the synthesized diff beyond the verified changed paths.
- Scaffold-only completion does NOT count: a candidate artifact without the named verification and downstream durability evidence is incomplete.

## Tasks

{tasks}
- [ ] Preserve the candidate lineage from `{CANDIDATE_JSON}` in the delivery record.

## Acceptance Criteria

- [ ] `{named_gate}` passes for the delivered diff with evidence matching `{verification.get('evidence_hash')}`.
- [ ] Scope and secret gates cover exactly the candidate commit and changed-path hash `{((verification.get('evidence') or {}).get('scope') or {}).get('changed_paths_hash')}`.
- [ ] The delivery uses one reviewable issue/PR candidate and is not auto-merged.
- [ ] Post-merge outcome ingestion records pending then durable/reverted status against the synthesis lineage.

## Implementation Notes

- Candidate id and complete machine lineage are stored in `{CANDIDATE_JSON}`.
- Workflows auto-pilot/Keepalive remains the only delivery controller.
- Lineage summary: `{json.dumps(lineage_summary, sort_keys=True)}`
"""


def compile_candidate(
    state: dict, exp_dir: str | Path, now: int | None = None
) -> tuple[dict, dict]:
    if state.get("delivery_phase") != "synth_verified":
        raise ValueError("candidate compilation requires synth_verified")
    root = Path(exp_dir)
    body = _candidate_body(state, root)
    candidate_id = (
        "synth-candidate:"
        + hashlib.sha256(
            f"{state['experiment_id']}|{(state.get('synthesis') or {}).get('commit')}|{state['verification'].get('evidence_hash')}".encode()
        ).hexdigest()[:24]
    )
    body_hash = _hash(body)
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "kind": "issue_or_pr_candidate",
        "repo": state.get("repo"),
        "title": f"Deliver verified synthesis from experiment {state['experiment_id']}",
        "body_path": str(root / CANDIDATE_BODY),
        "body_hash": body_hash,
        "lineage": state["lineage"],
        "synthesis": {
            "run_ids": (state.get("synthesis") or {}).get("run_ids") or [],
            "commit": (state.get("synthesis") or {}).get("commit"),
            "base_member_id": (state.get("synthesis") or {}).get("base"),
        },
        "verification_evidence_hash": state["verification"].get("evidence_hash"),
        "delivery": {
            "controller": "Workflows auto-pilot/Keepalive",
            "direct_publication_allowed": False,
            "auto_merge_allowed": False,
        },
        "created_ts": int(now or time.time()),
    }
    json_path = root / CANDIDATE_JSON
    body_path = root / CANDIDATE_BODY
    if json_path.exists():
        prior = json.loads(json_path.read_text(encoding="utf-8"))
        if prior.get("candidate_id") != candidate_id or prior.get("body_hash") != body_hash:
            raise ValueError("existing synthesis candidate conflicts with deterministic candidate")
        candidate = prior
    else:
        _atomic_text(body_path, body)
        _atomic_json(json_path, candidate)
    state = json.loads(json.dumps(state))
    state["candidate"] = candidate
    state, _ = transition(
        state,
        "candidate_ready",
        reason="verified_candidate_compiled",
        evidence={"candidate_id": candidate_id, "body_hash": body_hash},
        now=now,
    )
    return state, candidate


def link_delivery(
    exp_dir: str | Path,
    *,
    delivery_run_id: str,
    delivery_ref: str,
    pr_number: int | None = None,
    now: int | None = None,
) -> dict:
    if not delivery_run_id or not delivery_ref:
        raise ValueError("delivery link requires run id and issue/PR reference")
    root = Path(exp_dir)
    with _locked(root):
        state = load_state(root)
        if state is None or state.get("delivery_phase") not in {
            "candidate_ready",
            "delegated_or_pr",
            "merged",
        }:
            raise ValueError("delivery link requires a candidate_ready promotion")
        existing = state.get("delivery") or {}
        if existing and (
            existing.get("run_id") != delivery_run_id or existing.get("ref") != delivery_ref
        ):
            raise ValueError("conflicting delivery link")
        state["delivery"] = {
            "run_id": delivery_run_id,
            "ref": delivery_ref,
            "pr_number": pr_number,
            "linked_ts": int(now or time.time()),
        }
        if state["delivery_phase"] == "candidate_ready":
            state, _ = transition(
                state,
                "delegated_or_pr",
                reason="external_delivery_link_recorded",
                evidence=state["delivery"],
                now=now,
            )
        _atomic_json(state_path(root), state)
        return state


def default_outcome_lookup(state: dict) -> dict | None:
    run_id = (state.get("delivery") or {}).get("run_id")
    if not run_id or not Path(feedback.DB_PATH).exists():
        return None
    conn = sqlite3.connect(str(feedback.DB_PATH))
    try:
        row = conn.execute(
            "SELECT verifier_verdict,adjudicated_verdict,merged,ci_status,durability,durability_checked_ts "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "verifier_verdict": row[0],
        "adjudicated_verdict": row[1],
        "merged": bool(row[2]) if row[2] is not None else None,
        "ci_status": row[3],
        "durability": row[4],
        "durability_checked_ts": row[5],
    }


def default_mirror_outcome(state: dict, event_key: str) -> dict:
    synthesis = state.get("synthesis") or {}
    synth_run_id = synthesis.get("root_run_id") or synthesis.get("run_id")
    if not synth_run_id:
        return {"recorded": False, "reason": "no synthesis run id"}
    outcome = state.get("outcome") or {}
    phase = state.get("delivery_phase")
    if phase == "discarded":
        verifier = "FAIL_SYNTHESIS_PROMOTION"
        adjudicated = "FAIL"
        merged = bool(outcome.get("merged"))
        durability = outcome.get("durability") or "reworked"
    else:
        verifier = outcome.get("verifier_verdict") or "PASS_SYNTHESIS_PROMOTION"
        adjudicated = outcome.get("adjudicated_verdict") or "PASS"
        merged = bool(outcome.get("merged"))
        durability = outcome.get("durability") or ("durable" if phase == "durable" else "pending")
    feedback.record_outcome(
        synth_run_id,
        verifier_verdict=verifier,
        adjudicated_verdict=adjudicated,
        merged=merged,
        ci_status=outcome.get("ci_status"),
        durability=durability,
        notes=f"synthesis promotion {event_key}: phase={phase}",
    )
    members = state.get("lineage", {}).get("members") or []
    base = synthesis.get("base")
    edges = []
    for member in members:
        source_run_id = member.get("source_run_id")
        if not source_run_id:
            continue
        edge = feedback.record_influence_edge(
            target_run_id=synth_run_id,
            influence_type="experiment",
            influence_id=f"{state['experiment_id']}:{member.get('member_id')}",
            source_run_id=source_run_id,
            accepted=member.get("member_id") == base,
            acceptance_gate_id=state.get("verification", {}).get("evidence_hash"),
            metadata={"status": phase},
        )
        edges.append(edge["edge_id"])
        feedback.record_completion_event(
            source_run_id,
            event_type="delivery" if phase != "durable" else "durability",
            phase="delivery" if phase != "durable" else "durability",
            producer="exp_abcd",
            status=durability if phase in {"durable", "discarded"} else "merged",
            payload={
                "artifact_refs": [
                    {
                        "artifact_id": state.get("candidate", {}).get("candidate_id") or event_key,
                        "kind": "synthesis-candidate",
                        "ref_class": "delivery",
                    }
                ],
                "delivery": {
                    "target_id": (state.get("delivery") or {}).get("ref"),
                    "pr_number": (state.get("delivery") or {}).get("pr_number"),
                    "merged": merged,
                },
                "durability": {"status": durability},
                "result": {"outcome_verdict": adjudicated},
            },
        )
    return {"recorded": True, "synthesis_run_id": synth_run_id, "edge_ids": edges}


def _mirror_once(
    state: dict,
    event_key: str,
    mirror_fn: Callable[[dict, str], dict] | None,
) -> dict:
    if event_key in state.get("mirrored_events", []):
        return state
    result = (mirror_fn or default_mirror_outcome)(state, event_key)
    state = json.loads(json.dumps(state))
    state.setdefault("mirrored_events", []).append(event_key)
    state.setdefault("mirror_results", {})[event_key] = result
    return state


def reconcile(
    exp_dir: str | Path,
    *,
    launch_fn: Callable[[], dict] | None = None,
    completion_fn: Callable[[dict], dict] | None = None,
    resume_fn: Callable[[dict], dict] | None = None,
    verify_fn: Callable[[dict, Path], dict] | None = None,
    outcome_lookup_fn: Callable[[dict], dict | None] | None = None,
    mirror_fn: Callable[[dict, str], dict] | None = None,
    now: int | None = None,
    max_steps: int = 6,
) -> dict:
    root = Path(exp_dir)
    ts = int(now or time.time())
    with _locked(root):
        state = load_state(root)
        if state is None:
            raise ValueError("promotion state is not initialized")
        actions = []
        for _ in range(max(1, int(max_steps))):
            phase = state["delivery_phase"]
            if phase in TERMINAL_PHASES:
                break
            if ts >= int(state.get("expires_ts") or 0):
                state, _ = transition(state, "discarded", reason="promotion_expired", now=ts)
                state = _mirror_once(state, "expired", mirror_fn)
                actions.append("retired_expired")
                break
            if phase == "candidate_ready" and ts >= int(state.get("candidate_expires_ts") or 0):
                state, _ = transition(state, "discarded", reason="stale_candidate_retired", now=ts)
                state = _mirror_once(state, "candidate_retired", mirror_fn)
                actions.append("retired_stale_candidate")
                break
            next_retry = (state.get("retry") or {}).get("next_retry_ts")
            if next_retry and ts < int(next_retry):
                actions.append("retry_backoff")
                break

            if phase == "evaluated":
                if launch_fn is None:
                    actions.append("awaiting_synthesis_launch")
                    break
                launch_intent_id = (
                    "launch-intent:"
                    + hashlib.sha256(f"{state['experiment_id']}|synthesis".encode()).hexdigest()[
                        :24
                    ]
                )
                state.setdefault("synthesis", {}).update(
                    {
                        "launch_intent_id": launch_intent_id,
                        "launch_intent_ts": state.get("synthesis", {}).get("launch_intent_ts")
                        or ts,
                    }
                )
                # Durable intent precedes the side effect. exp_abcd.synthesize has
                # its own deterministic launch artifact, so a crash after spawn
                # returns the prior pid/run rather than spawning a duplicate.
                _atomic_json(state_path(root), state)
                launch = launch_fn()
                if launch.get("discard"):
                    state["synthesis"] = {
                        "gate": launch.get("gate"),
                        "ranking": launch.get("ranking"),
                    }
                    state, _ = transition(
                        state,
                        "discarded",
                        reason="usefulness_gate_discard",
                        evidence=launch,
                        now=ts,
                    )
                    actions.append("discarded_by_usefulness_gate")
                    break
                if launch.get("blocked") or not launch.get("pid"):
                    state = _record_retry(
                        state, launch.get("reason") or "synthesis launch blocked", ts
                    )
                    if state["retry"]["count"] >= state["max_retries"]:
                        state, _ = transition(
                            state, "discarded", reason="synthesis_launch_retries_exhausted", now=ts
                        )
                        state = _mirror_once(state, "launch_failed", mirror_fn)
                    actions.append("synthesis_launch_retry")
                    break
                run_id = launch.get("run_id") or f"{state['experiment_id']}:synth"
                worktree = Path(str(launch.get("worktree")))
                head = _git(worktree, ["rev-parse", "HEAD"])
                state["synthesis"] = {
                    "launch_intent_id": launch_intent_id,
                    "launch_intent_ts": state.get("synthesis", {}).get("launch_intent_ts") or ts,
                    "root_run_id": run_id,
                    "current_run_id": run_id,
                    "run_ids": [run_id],
                    "pid": launch.get("pid"),
                    "worktree": str(worktree),
                    "log": launch.get("log"),
                    "base": launch.get("base"),
                    "synth_agent": launch.get("synth_agent"),
                    "gate": launch.get("gate"),
                    "ranking": launch.get("ranking") or [],
                    "launch_head": head.stdout.strip() if head.returncode == 0 else None,
                    "resume_history": [],
                }
                state, _ = transition(
                    state,
                    "synth_running",
                    reason="synthesis_process_launched",
                    evidence={"run_id": run_id},
                    now=ts,
                )
                actions.append("synthesis_launched")
                break

            if phase == "synth_running":
                completion = (completion_fn or default_completion_probe)(state)
                status = completion.get("status")
                if status == "pending":
                    actions.append("synthesis_still_running")
                    break
                if status == "interrupted":
                    state = _record_retry(
                        state, completion.get("reason") or "synthesis interrupted", ts
                    )
                    if state["retry"]["count"] >= state["max_retries"]:
                        state, _ = transition(
                            state,
                            "discarded",
                            reason="synthesis_resume_retries_exhausted",
                            evidence=completion,
                            now=ts,
                        )
                        state = _mirror_once(state, "resume_failed", mirror_fn)
                        actions.append("synthesis_retired_after_interruptions")
                    elif resume_fn is not None:
                        resumed = resume_fn(state)
                        if resumed.get("pid") and resumed.get("run_id"):
                            synthesis = state["synthesis"]
                            resume_event = (
                                "resume:"
                                + hashlib.sha256(
                                    f"{state['experiment_id']}|{resumed['run_id']}".encode()
                                ).hexdigest()[:24]
                            )
                            if not any(
                                row.get("event_id") == resume_event
                                for row in synthesis.get("resume_history") or []
                            ):
                                synthesis.setdefault("resume_history", []).append(
                                    {
                                        "event_id": resume_event,
                                        "run_id": resumed["run_id"],
                                        "ts": ts,
                                    }
                                )
                                synthesis.setdefault("run_ids", []).append(resumed["run_id"])
                            synthesis.update(
                                {
                                    "current_run_id": resumed["run_id"],
                                    "pid": resumed["pid"],
                                    "log": resumed.get("log") or synthesis.get("log"),
                                }
                            )
                            state["retry"]["next_retry_ts"] = None
                            actions.append("synthesis_resumed")
                        else:
                            actions.append("synthesis_resume_failed")
                    else:
                        actions.append("synthesis_resume_required")
                    break
                if status == "failed":
                    state, _ = transition(
                        state,
                        "discarded",
                        reason=completion.get("reason") or "synthesis completion invalid",
                        evidence=completion,
                        now=ts,
                    )
                    state = _mirror_once(state, "synthesis_invalid", mirror_fn)
                    actions.append("synthesis_discarded")
                    break
                if status != "complete":
                    raise ValueError(f"unknown synthesis completion status={status!r}")
                state["synthesis"]["commit"] = completion.get("commit")
                state["synthesis"]["completion_evidence"] = completion
                state["retry"]["next_retry_ts"] = None
                state, _ = transition(
                    state,
                    "synth_complete",
                    reason="synthesis_commit_complete",
                    evidence=completion,
                    now=ts,
                )
                actions.append("synthesis_complete")
                continue

            if phase == "synth_complete":
                verification = (verify_fn or verify_synthesis)(state, root)
                state["verification"] = verification
                if not verification.get("passed"):
                    if verification.get("transient"):
                        state = _record_retry(
                            state,
                            verification.get("failure_reason") or "transient verification failure",
                            ts,
                        )
                        if state["retry"]["count"] >= state["max_retries"]:
                            state, _ = transition(
                                state,
                                "discarded",
                                reason="verification_retries_exhausted",
                                evidence=verification,
                                now=ts,
                            )
                            state = _mirror_once(state, "verification_failed", mirror_fn)
                    else:
                        state, _ = transition(
                            state,
                            "discarded",
                            reason="deterministic_verification_failure",
                            evidence=verification,
                            now=ts,
                        )
                        state = _mirror_once(state, "verification_failed", mirror_fn)
                    actions.append("verification_failed")
                    break
                state, _ = transition(
                    state,
                    "synth_verified",
                    reason="all_synthesis_gates_passed",
                    evidence=verification,
                    now=ts,
                )
                actions.append("synthesis_verified")
                continue

            if phase == "synth_verified":
                state, candidate = compile_candidate(state, root, now=ts)
                actions.append(f"candidate_ready:{candidate['candidate_id']}")
                continue

            if phase == "candidate_ready":
                # Only an explicit external delivery link may cross this boundary.
                actions.append("awaiting_external_delivery_link")
                break

            if phase in {"delegated_or_pr", "merged"}:
                outcome = (outcome_lookup_fn or default_outcome_lookup)(state)
                if not outcome:
                    actions.append("awaiting_delivery_outcome")
                    break
                state["outcome"] = outcome
                durability = outcome.get("durability")
                if durability in FAILURE_DURABILITY or (
                    outcome.get("merged") is False
                    and outcome.get("ci_status") in {"closed", "failed"}
                ):
                    state, _ = transition(
                        state,
                        "discarded",
                        reason=f"delivery_{durability or 'failed'}",
                        evidence=outcome,
                        now=ts,
                    )
                    state = _mirror_once(state, "delivery_failed", mirror_fn)
                    actions.append("delivery_retired")
                    break
                if phase == "delegated_or_pr" and outcome.get("merged") is True:
                    state, _ = transition(
                        state,
                        "merged",
                        reason="delivery_merged_pending_durability",
                        evidence=outcome,
                        now=ts,
                    )
                    state = _mirror_once(state, "merged", mirror_fn)
                    actions.append("delivery_merged")
                    continue
                if phase == "merged" and durability == "durable":
                    state, _ = transition(
                        state, "durable", reason="delivery_durably_held", evidence=outcome, now=ts
                    )
                    state = _mirror_once(state, "durable", mirror_fn)
                    actions.append("delivery_durable")
                    break
                actions.append("awaiting_durability")
                break
        _atomic_json(state_path(root), state)
        return {"state": state, "actions": actions}


def selftest() -> None:
    import shutil

    with tempfile.TemporaryDirectory(prefix="synthesis-promotion-") as tmp:
        old_feedback_db = feedback.DB_PATH
        feedback.DB_PATH = Path(tmp) / "feedback.db"
        root = Path(tmp) / "exp-1"
        root.mkdir()
        (root / "meta.json").write_text(
            json.dumps(
                {
                    "repo": "owner/repo",
                    "base": "main",
                    "base_sha": "base",
                    "agents": ["codex", "cursor"],
                    "exp_id": "exp-1",
                }
            )
        )
        (root / "spec.md").write_text("## Scope\n- change x\n")
        (root / "eval-maps.json").write_text('{"judge-a": {}}')
        state = ensure_evaluated_state(root, now=100)
        assert state["delivery_phase"] == "evaluated"
        calls = {"launch": 0, "resume": 0}
        wt = Path(tmp) / "worktree"
        wt.mkdir()
        subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wt), "config", "user.email", "test@example.test"], check=True
        )
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Test"], check=True)
        (wt / "x.py").write_text("x=1\n")
        subprocess.run(["git", "-C", str(wt), "add", "x.py"], check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "base"], check=True, capture_output=True
        )

        def launch():
            calls["launch"] += 1
            return {
                "pid": 999999,
                "run_id": "exp-1:synth",
                "worktree": str(wt),
                "log": str(root / "synth.log"),
                "base": "codex",
                "synth_agent": "codex",
            }

        launched = reconcile(root, launch_fn=launch, now=101)
        assert launched["state"]["delivery_phase"] == "synth_running"
        assert calls["launch"] == 1

        def interrupted(_state):
            return {"status": "interrupted", "reason": "simulated process loss"}

        def resume(_state):
            calls["resume"] += 1
            return {
                "pid": 999998,
                "run_id": "exp-1:synth:resume:1",
                "log": str(root / "resume.log"),
            }

        resumed = reconcile(root, completion_fn=interrupted, resume_fn=resume, now=102)
        assert resumed["state"]["delivery_phase"] == "synth_running"
        assert calls["resume"] == 1
        # The resumed attempt is now live; polling it cannot duplicate the resume.
        again = reconcile(
            root,
            completion_fn=lambda _state: {"status": "pending"},
            resume_fn=resume,
            now=103,
        )
        assert calls["resume"] == 1 and again["actions"] == ["synthesis_still_running"]

        (wt / "x.py").write_text("x=2\n")
        subprocess.run(["git", "-C", str(wt), "add", "x.py"], check=True)
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "synth"], check=True, capture_output=True
        )
        commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        def complete(_state):
            return {"status": "complete", "commit": commit, "marker_hash": "sha256:" + "a" * 64}

        def verified(_state, _root):
            evidence = {
                "scope": {
                    "ok": True,
                    "changed_paths": ["x.py"],
                    "changed_paths_hash": _hash(["x.py"]),
                },
                "secret_scan": {"ok": True, "finding_ids": []},
                "local_verify": {
                    "ok": True,
                    "verdict": "PASS",
                    "test_cmd": "pytest tests/test_x.py",
                },
                "runtime_ac": {"ok": True, "verdict": "PASS"},
                "repo_gates": [{"ok": True, "argv": ["pytest", "tests/test_x.py"]}],
                "deliberate_break_status": "PASS",
            }
            return {
                "passed": True,
                "transient": False,
                "evidence": evidence,
                "evidence_hash": _hash(evidence),
            }

        ready = reconcile(root, completion_fn=complete, verify_fn=verified, now=1000)
        assert ready["state"]["delivery_phase"] == "candidate_ready", ready
        candidate_id = ready["state"]["candidate"]["candidate_id"]
        repeat = reconcile(root, completion_fn=complete, verify_fn=verified, now=1001)
        assert repeat["state"]["candidate"]["candidate_id"] == candidate_id
        assert (
            len([row for row in repeat["state"]["phase_history"] if row["to"] == "candidate_ready"])
            == 1
        )
        shutil.rmtree(wt, ignore_errors=True)
        feedback.DB_PATH = old_feedback_db
    print("synthesis_promotion.py selftest: OK")


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities

        capabilities.production_heartbeat(
            "synthesis-promotion", event_type, ref="synthesis_promotion.main"
        )
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _capability_heartbeat()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("exp_dir", type=Path)
    status = sub.add_parser("status")
    status.add_argument("exp_dir", type=Path)
    rec = sub.add_parser("reconcile")
    rec.add_argument("exp_dir", type=Path)
    link = sub.add_parser("link-delivery")
    link.add_argument("exp_dir", type=Path)
    link.add_argument("--run-id", required=True)
    link.add_argument("--ref", required=True)
    link.add_argument("--pr-number", type=int)
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        selftest()
        return 0
    if args.command == "init":
        result = ensure_evaluated_state(args.exp_dir)
    elif args.command == "status":
        result = load_state(args.exp_dir) or {"status": "missing"}
    elif args.command == "reconcile":
        result = reconcile(args.exp_dir)
    else:
        result = link_delivery(
            args.exp_dir,
            delivery_run_id=args.run_id,
            delivery_ref=args.ref,
            pr_number=args.pr_number,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
