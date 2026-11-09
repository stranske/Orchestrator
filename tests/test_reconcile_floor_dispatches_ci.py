"""The floor-reconcile commit gets its own ci.yml verdict, without a reconcile loop.

A GITHUB_TOKEN push starts no workflow, so the reconcile commit used to have no CI run, and the
CI-gated mirror deploy (green ci.yml on main's exact HEAD) could not take main after any PR that
added tests (2026-11-09: #1269 at af0bedc, reconcile 3815ef1). The reconcile job now dispatches
ci.yml after its push; the dispatched run is `workflow_dispatch`, which the reconcile job skips.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _reconcile() -> dict:
    return _workflow()["jobs"]["reconcile-floor"]


def _step(name_prefix: str) -> dict:
    return next(s for s in _reconcile()["steps"] if str(s.get("name", "")).startswith(name_prefix))


def test_ci_can_be_dispatched():
    triggers = _workflow()[True]  # PyYAML reads the bare `on:` key as boolean True
    assert "workflow_dispatch" in triggers


def test_reconcile_runs_only_on_push_to_main_so_the_dispatched_run_cannot_loop():
    assert _reconcile()["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def test_reconcile_dispatches_ci_on_main_after_it_pushes():
    job = _reconcile()
    assert job["permissions"] == {"contents": "write", "actions": "write"}
    names = [s.get("name") for s in job["steps"]]
    commit = names.index("Commit the reconciled floor")
    dispatch = names.index("Verify the reconcile commit (dispatch ci.yml on main)")
    assert dispatch == commit + 1
    step = job["steps"][dispatch]
    assert step["if"] == "steps.commit.outputs.pushed == 'true'"
    assert job["steps"][commit]["id"] == "commit"
    assert step["run"].strip() == 'gh workflow run ci.yml --repo "$GITHUB_REPOSITORY" --ref main'
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "|| true" not in step["run"], "a refused dispatch must fail the job"


def _run_commit_step(tmp_path: Path, *, floor_changed: bool, push_rc: int = 0) -> tuple[int, str]:
    """Execute the real commit step against a scratch repo and a fake `git push`."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(args, cwd=repo, check=True, capture_output=True, text=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (repo / ".verify-floor.json").write_text('{"collected": 1}\n')
    run("git", "add", ".")
    run("git", "commit", "-qm", "base")
    if floor_changed:
        (repo / ".verify-floor.json").write_text('{"collected": 2}\n')
    output = tmp_path / "github_output"
    output.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    (bin_dir / "git").write_text(
        f'#!/bin/bash\n[ "$1" = push ] && exit {push_rc}\nexec {real_git} "$@"\n'
    )
    (bin_dir / "git").chmod(0o755)
    script = _step("Commit the reconciled floor")["run"]
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(output), "HOME": str(tmp_path)}
    done = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True)
    return done.returncode, output.read_text()


def test_a_reconcile_that_pushes_requests_the_dispatch(tmp_path):
    rc, out = _run_commit_step(tmp_path, floor_changed=True)
    assert rc == 0 and "pushed=true" in out


def test_nothing_to_reconcile_dispatches_nothing(tmp_path):
    rc, out = _run_commit_step(tmp_path, floor_changed=False)
    assert rc == 0 and "pushed=true" not in out


def test_a_refused_push_fails_the_step_and_requests_no_dispatch(tmp_path):
    rc, out = _run_commit_step(tmp_path, floor_changed=True, push_rc=1)
    assert rc != 0 and "pushed=true" not in out
