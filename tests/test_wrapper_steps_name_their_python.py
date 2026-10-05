"""The detached wrappers run their own python steps under the dispatcher's interpreter (2026-10-05).

THE DEFECT. The dispatch wrapper and the experiment wrapper are `bash -lc` strings, and their claim
release and completion step were spelled `python3 ...`. A login shell builds PATH from the profile,
so `python3` was whatever the profile put first. On the owner's machine that depended on the
LAUNCHER's environment. Codex Desktop exports conda's variables (`CONDA_SHLVL=1`, `CONDA_PREFIX`)
into every command it runs. With those set, the profile's conda hook took base as already active and
did not put `/opt/anaconda3/bin` back at the front. An older profile line's `~/anaconda/bin` stayed
first, and its `python3` is an unsigned x86_64 2016 interpreter that macOS SIGKILLs at exec. All
31 rc=137 dispatch runs show it: bash printed `Killed: 9` for the release and then for the
completion step within the marker's own second. 27 of them were launched from Codex Desktop
threads (the other 4, in July, left no launcher record), against 0 of 35 launched from Claude
sessions or `codex exec`, and the 3 Codex Desktop launches that went through `launchctl submit` (a
clean environment) were spared.

THE RULE. A wrapper names the interpreter of its python steps (`adapters.wrapper_python`, the one
running the dispatcher) and never asks the login shell for one. The tests below rebuild the live
mechanism with no dependence on this machine: a HOME whose login profile puts a `python3` that
SIGKILLs itself at the front of PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import adapters
import dispatcher
import exp_abcd
import feedback
import rate_incidents

TARGET = "o/r#9"
RUN = "o__r_9-cursor-1"


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A private Brain, capacity ledger, claims dir, incident authority and dispatch log dir."""
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "dispatch-logs")
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    return tmp_path


def _poisoned_home(world: Path) -> Path:
    """A HOME whose login profile puts a dead `python3` first, the way `~/.bash_profile` put
    `~/anaconda/bin` first. The stand-in kills itself at start, as macOS kills the real one."""
    dead = world / "dead-anaconda" / "bin"
    dead.mkdir(parents=True)
    python3 = dead / "python3"
    python3.write_text("#!/bin/sh\nkill -KILL $$\n")
    python3.chmod(0o755)
    home = world / "home"
    home.mkdir()
    (home / ".bash_profile").write_text(f'export PATH="{dead}:$PATH"\n')
    return home


def _login_env(world: Path, home: Path) -> dict:
    """The wrapper's environment: every store it can write points into the test's directory, and
    its login profile is the poisoned one."""
    return {
        **os.environ,
        "HOME": str(home),
        "HANDOFF_DIR": str(world),
        "ORCH_STATE_DIR": str(world / "state"),
        "ORCH_LOCAL_RUNTIME": str(world / "runtime"),
        "ORCH_FEEDBACK_DB": str(world / "t.db"),
        "ORCH_CAPABILITIES_PATH": str(world / "runtime" / "capabilities.json"),
        "ORCH_PUSH_RECORD_DISABLED": "1",
    }


def _assert_the_profile_kills_a_looked_up_python3(env: dict, cwd: Path) -> None:
    """The world must reproduce the live failure, or a pass below would prove nothing. The trailing
    echo keeps bash from exec-ing python3 in its own place, so the child's status is reported."""
    probe = subprocess.run(
        ["bash", "-lc", 'python3 -c pass; echo "rc=$?"'],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "rc=137" in probe.stdout, ("the poisoned profile must kill a looked-up python3", probe)


def _stub_steps(monkeypatch, world: Path) -> Path:
    """Stand-ins for the claims release and the completion step that record that they ran."""
    stubs = world / "stubs"
    stubs.mkdir()
    calls = stubs / "calls.log"
    for name, step in (("claims.py", "release"), ("ledger_reconcile.py", "complete")):
        (stubs / name).write_text(
            f"import sys\nopen({str(calls)!r}, 'a').write({step!r} + ' ' + ' '.join(sys.argv[2:])"
            " + '\\n')\n"
        )
    monkeypatch.setattr(dispatcher, "CLAIMS_PY", stubs / "claims.py")
    monkeypatch.setattr(dispatcher, "ORCH_DIR", stubs)
    monkeypatch.setattr(exp_abcd, "ORCH", stubs)
    return calls


def _dispatch_wrapper(monkeypatch, world: Path) -> tuple[dict, list[str]]:
    """`_spawn` exactly as the dispatcher runs it, the agent replaced by `(exit 0)` and the process
    start captured instead of launched."""
    seen: dict = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(argv, **_kw):
        seen["argv"] = argv
        return FakeProcess()

    cwd = world / "wt"
    cwd.mkdir(exist_ok=True)
    d = {
        "run_id": RUN,
        "agent": "cursor",
        "mode": "composer",
        "target": TARGET,
        "lane": "closer",
        "task_type": "implement",
        "model": "cursor:composer-2.5",
        "cwd": str(cwd),
        "wrapped": "(exit 0)",
    }
    # `dispatcher.subprocess` IS the subprocess module: scope the fake to this one call.
    with monkeypatch.context() as scoped:
        scoped.setattr(dispatcher.subprocess, "Popen", fake_popen)
        dispatcher._spawn(d)
    return d, seen["argv"]


def _steps(calls: Path) -> list[str]:
    return [line.split()[0] for line in calls.read_text().splitlines()] if calls.exists() else []


def test_a_login_profile_that_puts_a_dead_python3_first_cannot_kill_the_dispatch_steps(
    monkeypatch, world
):
    """The live shape of all 31 rc=137 runs, run as the dispatcher runs it: `bash -lc <wrapper>`."""
    calls = _stub_steps(monkeypatch, world)
    env = _login_env(world, _poisoned_home(world))
    d, argv = _dispatch_wrapper(monkeypatch, world)
    assert argv[:2] == ["bash", "-lc"], argv
    _assert_the_profile_kills_a_looked_up_python3(env, Path(d["cwd"]))
    log = dispatcher.DISPATCH_LOG_DIR / "o__r_9.cursor.log"
    with log.open("a") as fh:
        done = subprocess.run(
            argv, cwd=d["cwd"], env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=120
        )
    assert done.returncode == 0, log.read_text()
    assert "Killed" not in log.read_text(), log.read_text()
    assert _steps(calls) == ["release", "complete"], ("both steps must run", log.read_text())
    marker = json.loads((dispatcher.DISPATCH_LOG_DIR / "done" / f"{RUN}.json").read_text())
    assert (marker["rc"], marker["release_rc"]) == (0, 0), marker


def test_the_experiment_wrappers_completion_survives_the_same_profile(monkeypatch, world):
    """`exp_abcd` builds the same `bash -lc` wrapper; audit F2 saw this step killed 522 times."""
    calls = _stub_steps(monkeypatch, world)
    env = _login_env(world, _poisoned_home(world))
    _assert_the_profile_kills_a_looked_up_python3(env, world)
    complete = exp_abcd._completion_cmd(
        "codex", "full", "exp:e1:codex:1", "exp:repo", "implement", world / "exp.log", 1
    )
    done = subprocess.run(
        ["bash", "-lc", complete], cwd=world, env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done
    assert _steps(calls) == ["complete"], done


def test_the_steps_interpreter_is_the_one_running_the_dispatcher():
    assert adapters.wrapper_python() == sys.executable


@pytest.mark.parametrize("executable", ["", "python3", "/no/such/interpreter"])
def test_an_interpreter_that_cannot_name_itself_falls_back_to_the_path_lookup(
    monkeypatch, executable
):
    monkeypatch.setattr(sys, "executable", executable)
    assert adapters.wrapper_python() == "python3"
