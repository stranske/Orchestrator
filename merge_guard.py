#!/usr/bin/env python3
"""Guard terminal PR merges with the same runtime-AC gates used by tick.py.

Default mode is dry-run. Active mode requires --confirm-merge, and required
runtime-AC gates still require ORCH_RUN_RUNTIME_AC=1 before any checks run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import feedback
import provision
import runtime_ac_gate

MERGE_METHOD_FLAGS = {
    "squash": "--squash",
    "merge": "--merge",
    "rebase": "--rebase",
}


def build_merge_cmd(target: str, *, method: str = "squash", delete_branch: bool = False,
                    auto: bool = False) -> list[str]:
    repo, num = provision.parse_target(target)
    if num is None:
        raise ValueError(f"merge target must be a PR ref owner/repo#N: {target!r}")
    if method not in MERGE_METHOD_FLAGS:
        raise ValueError(f"unknown merge method {method!r}")
    cmd = ["gh", "pr", "merge", str(num), "-R", repo, MERGE_METHOD_FLAGS[method]]
    if delete_branch:
        cmd.append("--delete-branch")
    if auto:
        cmd.append("--auto")
    return cmd


def _label_names(labels: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(labels, list):
        return out
    for label in labels:
        if isinstance(label, dict) and label.get("name"):
            out.append(str(label["name"]))
        elif isinstance(label, str):
            out.append(label)
    return out


def pr_metadata(target: str, *, run_fn=subprocess.run) -> dict[str, Any]:
    repo, num = provision.parse_target(target)
    if num is None:
        return {"target": target, "error": "target does not contain a PR number"}
    cmd = [
        "gh", "pr", "view", str(num), "-R", repo,
        "--json", "title,labels,state,isDraft,mergeStateStatus",
    ]
    res = run_fn(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"target": target, "error": (res.stderr or "gh pr view failed")[-500:]}
    try:
        doc = json.loads(res.stdout or "{}")
    except Exception as exc:
        return {"target": target, "error": f"could not parse gh pr view JSON: {exc}"}
    return {
        "target": target,
        "title": doc.get("title") or "",
        "labels": _label_names(doc.get("labels")),
        "state": doc.get("state"),
        "is_draft": bool(doc.get("isDraft")),
        "merge_state_status": doc.get("mergeStateStatus"),
    }


def evaluate_merge_gate(
    target: str,
    *,
    dry_run: bool,
    env: dict | None = None,
    spec_dir: str | Path | None = None,
    require_runtime_ac: bool = False,
    metadata_fn=pr_metadata,
    gate_fn=runtime_ac_gate.gate_status,
) -> dict[str, Any]:
    meta = metadata_fn(target)
    base = {"target": target, "dry_run": dry_run, "metadata": meta, "gate": None,
            "blocked": False, "reason": None}
    if meta.get("error"):
        return {**base, "blocked": True, "reason": f"could not read PR metadata: {meta['error']}"}
    if str(meta.get("state") or "").upper() != "OPEN":
        return {**base, "blocked": True, "reason": f"PR state is {meta.get('state')!r}, not OPEN"}
    if meta.get("is_draft"):
        return {**base, "blocked": True, "reason": "PR is draft"}

    labels = list(meta.get("labels") or [])
    if require_runtime_ac and "runtime-ac" not in {label.lower() for label in labels}:
        labels.append("runtime-ac")
    item = {
        "target": target,
        "task_type": "implement",
        "lane": "closer",
        "labels": labels,
        "title": meta.get("title") or "",
    }
    gate = gate_fn(item, dry_run=dry_run, env=env, spec_dir=spec_dir)
    result = {**base, "gate": gate}
    if gate is None:
        return result
    if gate.get("blocks"):
        return {**result, "blocked": True, "reason": f"runtime AC gate {gate.get('status')}"}
    if dry_run and gate.get("status") == "missing_spec":
        return {**result, "blocked": True, "reason": "runtime AC gate would need a spec before active merge"}
    return result


def record_merge_outcome(
    target: str,
    *,
    latest_run_fn=feedback.latest_run_id_for_target,
    record_outcome_fn=feedback.record_outcome,
) -> dict[str, Any]:
    try:
        run_id = latest_run_fn(target, mode="remote")
        if not run_id:
            return {"recorded": False, "reason": "no remote run_id found for target"}
        record_outcome_fn(
            run_id,
            adjudicated_verdict="PASS",
            merged=True,
            durability="pending",
            notes="merge_guard: gh pr merge succeeded; durability pending sweep",
        )
        return {"recorded": True, "run_id": run_id}
    except Exception as exc:
        return {"recorded": False, "error": str(exc)}


def guarded_merge(
    target: str,
    *,
    method: str = "squash",
    delete_branch: bool = False,
    auto: bool = False,
    confirm_merge: bool = False,
    env: dict | None = None,
    spec_dir: str | Path | None = None,
    require_runtime_ac: bool = False,
    metadata_fn=pr_metadata,
    gate_fn=runtime_ac_gate.gate_status,
    merge_fn=subprocess.run,
    latest_run_fn=feedback.latest_run_id_for_target,
    record_outcome_fn=feedback.record_outcome,
) -> dict[str, Any]:
    cmd = build_merge_cmd(target, method=method, delete_branch=delete_branch, auto=auto)
    dry_run = not confirm_merge
    gate_result = evaluate_merge_gate(
        target,
        dry_run=dry_run,
        env=env,
        spec_dir=spec_dir,
        require_runtime_ac=require_runtime_ac,
        metadata_fn=metadata_fn,
        gate_fn=gate_fn,
    )
    result = {
        **gate_result,
        "merge_cmd": cmd,
        "merge_executed": False,
        "merge_returncode": None,
    }
    if gate_result["blocked"]:
        return result
    if dry_run:
        return result

    merge = merge_fn(cmd, capture_output=True, text=True)
    result["merge_executed"] = True
    result["merge_returncode"] = merge.returncode
    result["stdout_tail"] = (merge.stdout or "")[-1000:]
    result["stderr_tail"] = (merge.stderr or "")[-1000:]
    if merge.returncode != 0:
        result["blocked"] = True
        result["reason"] = "gh pr merge failed"
        return result
    result["outcome"] = record_merge_outcome(
        target,
        latest_run_fn=latest_run_fn,
        record_outcome_fn=record_outcome_fn,
    )
    return result


def _selftest() -> None:
    import tempfile

    assert build_merge_cmd("o/r#5") == ["gh", "pr", "merge", "5", "-R", "o/r", "--squash"]
    assert build_merge_cmd("o/r#5", method="rebase", delete_branch=True, auto=True) == [
        "gh", "pr", "merge", "5", "-R", "o/r", "--rebase", "--delete-branch", "--auto",
    ]

    open_meta = lambda target: {"target": target, "labels": [], "title": "ready", "state": "OPEN",
                                "is_draft": False}
    dry = guarded_merge("o/r#5", metadata_fn=open_meta)
    assert dry["merge_cmd"] and dry["merge_executed"] is False and dry["blocked"] is False, dry
    meta_fail = guarded_merge("o/r#5", metadata_fn=lambda target: {"target": target, "error": "no auth"})
    assert meta_fail["blocked"] is True and "metadata" in meta_fail["reason"], meta_fail
    draft = guarded_merge("o/r#5", metadata_fn=lambda target: {**open_meta(target), "is_draft": True})
    assert draft["blocked"] is True and draft["reason"] == "PR is draft", draft
    closed = guarded_merge("o/r#5", metadata_fn=lambda target: {**open_meta(target), "state": "CLOSED"})
    assert closed["blocked"] is True and "not OPEN" in closed["reason"], closed
    missing_state = guarded_merge("o/r#5", metadata_fn=lambda target: {"target": target, "labels": []})
    assert missing_state["blocked"] is True and "not OPEN" in missing_state["reason"], missing_state

    with tempfile.TemporaryDirectory(prefix="merge-guard-") as tmp:
        runtime_meta = lambda target: {"target": target, "labels": ["runtime-ac"], "title": "runtime",
                                       "state": "OPEN", "is_draft": False}
        missing = guarded_merge("o/r#5", confirm_merge=True, spec_dir=tmp, metadata_fn=runtime_meta,
                                merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")))
        assert missing["blocked"] is True and missing["gate"]["status"] == "missing_spec", missing

        path = runtime_ac_gate.spec_path("o/r#5", spec_dir=tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        # `_command_spec` is runtime_ac_gate's own exercise fixture: its verification.target is
        # pinned to stranske/Workflows#303, the target exercise_gate() uses. execute_gate() later
        # gained a spec-target/closer-target match guard, which made this fixture fail closed here
        # with `spec target ... does not match closer target 'o/r#5'` -- so the happy-path merge
        # assertion below could never be reached. Restate the target the way runtime_ac_gate's own
        # selftest does for its materialized spec. (2026-08-21)
        gate_spec = runtime_ac_gate._command_spec(
            str(Path(__file__).resolve().parent),
            f"{sys.executable} -c 'print(\"ok\")'",
        )
        gate_spec["verification"]["target"] = "o/r#5"
        path.write_text(json.dumps(gate_spec), encoding="utf-8")
        disabled = guarded_merge("o/r#5", confirm_merge=True, spec_dir=tmp, env={}, metadata_fn=runtime_meta,
                                 merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")))
        assert disabled["blocked"] is True and disabled["gate"]["status"] == "required_but_not_run", disabled
        forced = guarded_merge("o/r#6", confirm_merge=True, spec_dir=tmp, env={}, require_runtime_ac=True,
                               metadata_fn=open_meta,
                               merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")))
        assert forced["blocked"] is True and forced["gate"]["status"] == "missing_spec", forced

        calls = []

        def fake_merge(cmd, capture_output=True, text=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")

        recorded = []
        passed = guarded_merge(
            "o/r#5",
            confirm_merge=True,
            spec_dir=tmp,
            env={"ORCH_RUN_RUNTIME_AC": "1", "ORCH_RUNTIME_AC_ALLOW_COMMANDS": "1"},
            metadata_fn=runtime_meta,
            merge_fn=fake_merge,
            latest_run_fn=lambda target, mode=None: "remote:o/r#5:codex",
            record_outcome_fn=lambda run_id, **kwargs: recorded.append((run_id, kwargs)),
        )
        assert passed["blocked"] is False and passed["merge_executed"] is True, passed
        assert calls and recorded and recorded[0][1]["durability"] == "pending", (calls, recorded)
        no_run = record_merge_outcome("o/r#5", latest_run_fn=lambda target, mode=None: None)
        assert no_run["recorded"] is False and "no remote run_id" in no_run["reason"], no_run

        blocked = guarded_merge(
            "o/r#5",
            confirm_merge=True,
            metadata_fn=runtime_meta,
            gate_fn=lambda item, **kwargs: {"target": item["target"], "status": "executed",
                                            "verdict": "FAIL", "blocks": True},
            merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")),
        )
        assert blocked["blocked"] is True and blocked["reason"] == "runtime AC gate executed", blocked

    parsed_meta = pr_metadata(
        "o/r#5",
        run_fn=lambda cmd, capture_output=True, text=True: subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"title": "T", "state": "OPEN", "isDraft": False,
                               "mergeStateStatus": "CLEAN",
                               "labels": [{"name": "runtime-ac"}]}),
            stderr="",
        ),
    )
    assert parsed_meta["labels"] == ["runtime-ac"] and parsed_meta["title"] == "T", parsed_meta
    print("merge_guard.py selftest: OK (metadata, runtime AC gate, dry-run, guarded gh merge, outcome patch)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Guard gh pr merge with Orchestrator runtime AC gates.")
    parser.add_argument("target", nargs="?", help="PR target owner/repo#N")
    parser.add_argument("--method", choices=sorted(MERGE_METHOD_FLAGS), default="squash")
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument("--auto", action="store_true", help="pass --auto to gh pr merge")
    parser.add_argument("--confirm-merge", action="store_true", help="actually run gh pr merge")
    parser.add_argument("--require-runtime-ac", action="store_true",
                        help="force a runtime AC gate even without a runtime-ac label or spec")
    parser.add_argument("--json", action="store_true", help="accepted for consistency; output is always JSON")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if not args.target:
        parser.error("target is required unless --selftest is used")
    result = guarded_merge(
        args.target,
        method=args.method,
        delete_branch=args.delete_branch,
        auto=args.auto,
        confirm_merge=args.confirm_merge,
        require_runtime_ac=args.require_runtime_ac,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if not result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
