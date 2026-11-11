"""Two gh guard copies on PATH must reach the real gh, never exec each other forever.

2026-11-10: a run's guard and a test's guard copy were both on PATH; each wrapper skipped only its
own directory, so they exec'd one another in a loop (227 CPU-minutes each over 8.5 h) and their cwd
held a worktree "live", failing the closer's provisioning of Orchestrator#1336 twenty times.
"""

import os
import stat
import subprocess

import dispatcher


def _guard(root):
    for path, text in dispatcher._gh_guard_files(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    wrapper = root / "bin" / "gh"
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper


def test_two_guards_on_path_reach_the_real_gh(tmp_path):
    first = _guard(tmp_path / "guard-run")
    second = _guard(tmp_path / "guard-test")
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "gh"
    real.write_text('#!/bin/sh\necho "REAL $*"\n')
    real.chmod(real.stat().st_mode | stat.S_IXUSR)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCH_", "GH_"))}
    env["PATH"] = os.pathsep.join(
        [str(second.parent), str(first.parent), str(real_dir), "/usr/bin", "/bin"]
    )
    done = subprocess.run(
        [str(first), "auth", "token"],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "REAL auth token"
