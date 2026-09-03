#!/usr/bin/env python3
"""Shadow synthesis-promotion reconcile exercise (injected launch/verify, no live synth)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import synthesis_promotion  # noqa: E402

FIXTURE = Path(__file__).resolve().parent
OUT = FIXTURE / "reconcile-out.json"


def _exp_dir(mode: str) -> Path:
    return FIXTURE / f"exp-r15-synth-{mode}"


def _setup_exp(exp: Path) -> None:
    exp.mkdir(parents=True, exist_ok=True)
    (exp / "meta.json").write_text(
        json.dumps(
            {
                "repo": "owner/repo",
                "base": "main",
                "base_sha": "base",
                "agents": ["codex", "cursor"],
                "exp_id": exp.name,
            }
        )
    )
    (exp / "spec.md").write_text("## Scope\n- change x\n")
    (exp / "eval-maps.json").write_text('{"judge-a": {}}')


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "pass"
    exp = _exp_dir(mode)
    if exp.exists():
        import shutil

        shutil.rmtree(exp)
    _setup_exp(exp)
    synthesis_promotion.ensure_evaluated_state(exp, now=100)
    wt = FIXTURE / "worktree"
    if wt.exists():
        import shutil

        shutil.rmtree(wt)
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "Test"], check=True)
    (wt / "x.py").write_text("x=1\n")
    subprocess.run(["git", "-C", str(wt), "add", "x.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "base"], check=True, capture_output=True)

    def launch():
        return {
            "pid": 999999,
            "run_id": "exp-r15-synth:synth",
            "worktree": str(wt),
            "log": str(exp / "synth.log"),
            "base": "codex",
            "synth_agent": "codex",
        }

    def complete(_state):
        (wt / "x.py").write_text("x=2\n")
        subprocess.run(["git", "-C", str(wt), "add", "x.py"], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-m", "synth"], check=True, capture_output=True)
        commit = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return {"status": "complete", "commit": commit, "marker_hash": "sha256:" + "a" * 64}

    def verified(_state, _root):
        passed = mode != "break"
        evidence = {
            "scope": {"ok": True, "changed_paths": ["x.py"], "changed_paths_hash": synthesis_promotion._hash(["x.py"])},
            "secret_scan": {"ok": True, "finding_ids": []},
            "local_verify": {"ok": passed, "verdict": "PASS" if passed else "FAIL"},
            "runtime_ac": {"ok": True, "verdict": "PASS"},
            "repo_gates": [{"ok": passed, "argv": ["git", "diff", "--check"]}],
            "deliberate_break_status": "PASS" if passed else "FAIL",
        }
        return {
            "passed": passed,
            "transient": False,
            "evidence": evidence,
            "evidence_hash": synthesis_promotion._hash(evidence),
            "failure_reason": None if passed else "synthesis verification gates did not all pass",
        }

    launched = synthesis_promotion.reconcile(exp, launch_fn=launch, now=101)
    ready = synthesis_promotion.reconcile(
        exp,
        completion_fn=complete,
        verify_fn=verified,
        now=1000,
        max_steps=6,
    )
    OUT.write_text(json.dumps(ready, indent=2, sort_keys=True) + "\n")
    phase = ready["state"]["delivery_phase"]
    print(json.dumps({"delivery_phase": phase, "actions": ready.get("actions")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
