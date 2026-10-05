"""A dispatched agent's gh reads the config its dispatcher's gh reads. The pin is a path, never a token.

THE DEFECT (measured 2026-10-04). `dispatcher._agent_runtime_prelude` moves XDG_CONFIG_HOME into the
agent's runtime so the agent CLIs keep their mutable state out of the real home. gh honours
XDG_CONFIG_HOME too, so in every dispatched agent it read an empty `<runtime>/<agent>/.config/gh`,
printed "gh auth login" and exited 4 before it asked the keyring for a token. Over 90 days of
dispatch logs, 58 of the 58 delegated codex runs that called gh failed that way on their first
call, and 25 of them then read the token through `git credential fill` instead. cursor and gemini
runs reported the same failure in their own words.

THE FIX. The prelude exports GH_CONFIG_DIR, resolved by gh's own rule from the DISPATCHER's
environment (`dispatcher.gh_config_dir`). gh then takes the token from wherever that config says,
the keyring on the owner's machine, exactly as the dispatcher's gh does. Nothing new enters the
agent's argv or environment, and every other XDG-honouring tool keeps the runtime redirect.

The behavioural tests run the REAL gh and bash against a scratch config that holds a string that is
not a credential. HOME is in the sandbox and the environment is built from scratch, so neither the
owner's keychain nor an inherited token can reach them, and no test touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import dispatcher
import exp_abcd
import paths

AGENTS = ("codex", "cursor", "vibe", "gemini", "claude", "aider")
# Deliberately not token-shaped: no secret scanner should ever match the fixture.
FAKE = "orch-test-not-a-credential"
EXPORT = re.compile(r"export GH_CONFIG_DIR=('[^']*'|[^\s;]+);")
GH = shutil.which("gh")
NEEDS_GH = pytest.mark.skipif(
    GH is None, reason="gh is not installed on this machine, so its config lookup cannot be run"
)
TARGET = "o/r#8"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """The dispatcher's runtime and real home in tmp_path; no gh or XDG variable inherited."""
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "REAL_HOME", tmp_path / "home")
    for var in (
        "GH_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        dispatcher.AGENT_GH_CONFIG_DISABLED_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _pins(text: str) -> list[str]:
    return [shlex.split(value)[0] for value in EXPORT.findall(text)]


def _dispatcher_gh_config(sandbox: Path) -> Path:
    """A gh config the dispatcher's gh would use, holding a non-credential token."""
    config = sandbox / "dispatcher-xdg" / "gh"
    config.mkdir(parents=True)
    (config / "hosts.yml").write_text(
        f"github.com:\n    oauth_token: {FAKE}\n    user: orch-test-user\n"
        "    git_protocol: https\n"
    )
    return config


def _gh_env(sandbox: Path) -> dict[str, str]:
    """From scratch: no token, no gh or XDG variable, and HOME in the sandbox."""
    assert GH is not None
    home = sandbox / "gh-home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": f"{Path(GH).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PROMPT_DISABLED": "1",
        "NO_COLOR": "1",
    }


def _in_agent_shell(prelude: str, command: str, sandbox: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", prelude + command],
        env=_gh_env(sandbox),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_the_pin_follows_ghs_own_lookup_order(sandbox, monkeypatch):
    """GH_CONFIG_DIR, else $XDG_CONFIG_HOME/gh, else ~/.config/gh: gh's `config.ConfigDir`."""
    assert dispatcher.gh_config_dir() == sandbox / "home" / ".config" / "gh"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "xdg"))
    assert dispatcher.gh_config_dir() == sandbox / "xdg" / "gh"
    monkeypatch.setenv("GH_CONFIG_DIR", str(sandbox / "explicit"))
    assert dispatcher.gh_config_dir() == sandbox / "explicit"


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_prelude_pins_gh_to_its_dispatchers_config(sandbox, monkeypatch, agent):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "dispatcher-xdg"))
    prelude = dispatcher._agent_runtime_prelude(agent)
    assert _pins(prelude) == [str(sandbox / "dispatcher-xdg" / "gh")], prelude
    # Every other XDG-honouring tool keeps its config in the runtime, as before.
    runtime_config = sandbox / "agent-runtime" / agent / ".config"
    assert f"export XDG_CONFIG_HOME={shlex.quote(str(runtime_config))};" in prelude, prelude


@pytest.mark.parametrize("agent", AGENTS)
def test_the_pin_is_a_path_and_never_a_token(sandbox, monkeypatch, agent):
    """A dispatcher holding a token in its environment still writes no token into the prelude."""
    monkeypatch.setenv("GH_TOKEN", FAKE)
    monkeypatch.setenv("GITHUB_TOKEN", FAKE)
    prelude = dispatcher._agent_runtime_prelude(agent)
    assert FAKE not in prelude, prelude
    assert "GH_TOKEN" not in prelude and "GITHUB_TOKEN" not in prelude, prelude
    assert _pins(prelude) == [str(sandbox / "home" / ".config" / "gh")], prelude


@pytest.mark.parametrize("agent", AGENTS)
def test_the_kill_switch_removes_the_pin_and_nothing_else(sandbox, monkeypatch, agent):
    pinned = [part for part in dispatcher._agent_runtime_prelude(agent).split("; ") if part]
    monkeypatch.setenv(dispatcher.AGENT_GH_CONFIG_DISABLED_ENV, "1")
    unpinned = [part for part in dispatcher._agent_runtime_prelude(agent).split("; ") if part]
    assert not any("GH_CONFIG_DIR" in part for part in unpinned), unpinned
    dropped = [part for part in pinned if part not in unpinned]
    assert len(dropped) == 1 and dropped[0].startswith("export GH_CONFIG_DIR="), dropped
    assert [part for part in pinned if part != dropped[0]] == unpinned


@NEEDS_GH
def test_a_dispatched_agents_gh_authenticates_from_the_dispatchers_config(sandbox, monkeypatch):
    """The real gh, run under the real prelude, finds the dispatcher's config through the redirect."""
    _dispatcher_gh_config(sandbox)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "dispatcher-xdg"))

    pinned = _in_agent_shell(dispatcher._agent_runtime_prelude("codex"), "gh auth token", sandbox)
    assert (pinned.returncode, pinned.stdout.strip()) == (0, FAKE), (pinned.stdout, pinned.stderr)

    # Without the pin, gh reads the empty runtime config and stops before any API call. `gh api`
    # never prints a token, so this half cannot surface one even on a misconfigured machine.
    monkeypatch.setenv(dispatcher.AGENT_GH_CONFIG_DISABLED_ENV, "1")
    unpinned = _in_agent_shell(dispatcher._agent_runtime_prelude("codex"), "gh api user", sandbox)
    assert unpinned.returncode == 4, (unpinned.returncode, unpinned.stdout, unpinned.stderr)
    assert "gh auth login" in unpinned.stdout + unpinned.stderr, unpinned.stderr


def _child_env(sandbox: Path, **extra: str) -> dict[str, str]:
    """A child interpreter that imports the dispatcher with every runtime path in the sandbox.

    It inherits no ORCH_* variable, so nothing points it back at live state, and the registries
    that seed on a first load go to the sandbox, never MODULE_DIR, which the mirror deploys
    (test_gemini_workspace_one_spelling.py has the reasoning).
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ORCH_", "GH_", "XDG_"))
        and k not in ("HANDOFF_DIR", "CODEX_SANDBOX", "GITHUB_TOKEN")
    }
    runtime = sandbox / "runtime"
    env.update(
        HOME=str(sandbox / "home"),
        HANDOFF_DIR=str(sandbox / "handoff"),
        ORCH_LOCAL_RUNTIME=str(runtime),
        ORCH_STATE_DIR=str(runtime / "state"),
        ORCH_OFFLOAD_DIR=str(runtime / "offloads"),
        ORCH_MODEL_PROBE="0",
        ORCH_CODEX_BYPASS_INNER_SANDBOX="0",
        PYTHONPATH=str(paths.MODULE_DIR),
        **extra,
    )
    env.update(
        {var: str(sandbox / "registries" / name) for var, name in paths.SEEDED_REGISTRY_ENV.items()}
    )
    return env


def _run_child(sandbox: Path, script: str, *args: str, **extra: str) -> dict:
    seeded = [paths.MODULE_DIR / "experiments" / n for n in paths.SEEDED_REGISTRY_ENV.values()]
    absent = [path for path in seeded if not path.exists()]
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=sandbox,
        env=_child_env(sandbox, **extra),
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    created = [str(path) for path in absent if path.exists()]
    assert not created, f"the child seeded a registry inside MODULE_DIR: {created}"
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (proc.stdout[-3000:], proc.stderr[-3000:])
    return json.loads(lines[0][len("RESULT ") :])


NESTED = """
import json
import dispatcher
print("RESULT " + json.dumps(str(dispatcher.gh_config_dir())))
"""


def test_a_dispatcher_run_inside_a_dispatched_agent_pins_the_same_config(sandbox, monkeypatch):
    """An agent that offloads in turn hands ITS agent the same gh config, not its own runtime's.

    Inside the agent XDG_CONFIG_HOME names the runtime, so the order of gh's rule decides this:
    the explicit GH_CONFIG_DIR the prelude exported must win over the redirect.
    """
    expected = _dispatcher_gh_config(sandbox)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "dispatcher-xdg"))
    prelude = dispatcher._agent_runtime_prelude("codex")
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(NESTED)}"
    env = _child_env(sandbox)
    proc = subprocess.run(
        ["bash", "-c", prelude + command],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (proc.stdout[-2000:], proc.stderr[-2000:])
    assert json.loads(lines[0][len("RESULT ") :]) == str(expected)


PLAN = """
import json, subprocess, sys
import dispatcher

plan = dispatcher.plan_dispatch(
    {"agent": "codex", "target": sys.argv[1], "task_type": "implement", "mode": "full",
     "lane": "opener", "prompt": "Implement the change."},
    dry_run=True,
)

# offload runs the agent as `bash -lc <wrapped>`; only that call is faked.
seen, real_run = {}, subprocess.run

class Finished:
    returncode, stdout, stderr = 0, "OFFLOAD RESULT", ""

def run(cmd, *args, **kwargs):
    if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
        seen["wrapped"] = cmd[2]
        return Finished()
    return real_run(cmd, *args, **kwargs)

subprocess.run = run
off = dispatcher.offload("cursor", "Read the notes.", cwd=sys.argv[2], timeout=60)
print("RESULT " + json.dumps({"plan": plan["wrapped"], "plan_argv0": plan["argv"][0],
                              "offload": seen.get("wrapped"), "offload_result": off}))
"""


def _prelude_before(wrapped: str, argv0: str) -> str:
    """The shell text a wrapped command runs before the agent's argv, minus the agent itself."""
    marker = f"; {shlex.quote(argv0)} "
    assert wrapped.count(marker) == 1, (marker, wrapped[:800])
    prelude = wrapped[: wrapped.index(marker) + 2]
    # plan_dispatch runs the agent in a subshell, `<PATH>; (<prelude><argv>)`; offload does not.
    return prelude.split("; (", 1)[-1]


@NEEDS_GH
def test_the_dispatch_and_offload_preludes_authenticate_gh(sandbox):
    """The exact prelude bytes `plan_dispatch` and `offload` hand bash, run with the real gh.

    `plan_dispatch` is the path the 2026-10-04 batch took (`dispatcher.py delegate`), here for
    codex; `offload` is the highest-volume path, here for cursor, its busiest seat. The text each
    runs before the agent's argv is executed with `gh auth token` in the agent's place, so a path
    that ever built its prelude without the pin fails here.
    """
    expected = _dispatcher_gh_config(sandbox)
    source = sandbox / "source"
    source.mkdir()
    (source / "notes.txt").write_text("an offload reads this\n")
    out = _run_child(
        sandbox, PLAN, TARGET, str(source), XDG_CONFIG_HOME=str(sandbox / "dispatcher-xdg")
    )
    assert out["offload"], f"the faked offload run was never reached: {out['offload_result']}"
    for name, wrapped, argv0 in (
        ("plan_dispatch", out["plan"], out["plan_argv0"]),
        ("offload", out["offload"], "cursor-agent"),
    ):
        assert _pins(wrapped) == [str(expected)], (name, wrapped[:800])
        ran = _in_agent_shell(_prelude_before(wrapped, argv0), "gh auth token", sandbox)
        assert (ran.returncode, ran.stdout.strip()) == (0, FAKE), (name, ran.stdout, ran.stderr)


def test_both_experiment_paths_pin_gh(sandbox, monkeypatch, tmp_path):
    """exp_abcd builds its own wrapped commands for experiment arms and evaluators."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "dispatcher-xdg"))
    expected = [str(sandbox / "dispatcher-xdg" / "gh")]
    arm = exp_abcd._wrapped("codex", ["codex", "exec", "Implement the change."])
    evaluator = exp_abcd._eval_command("codex", str(tmp_path / "prompt.txt"))
    assert _pins(arm) == expected, arm
    assert _pins(evaluator) == expected, evaluator
