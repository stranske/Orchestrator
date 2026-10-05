"""A dispatched agent's gh reads the config its dispatcher's gh reads. The pin is a path, never a token.

THE DEFECT (measured 2026-10-04). `dispatcher._agent_runtime_prelude` moves XDG_CONFIG_HOME into the
agent's runtime so the agent CLIs keep their mutable state out of the real home. gh honours
XDG_CONFIG_HOME too, so in an agent whose dispatcher exported neither GH_CONFIG_DIR nor GH_TOKEN it
read an empty `<runtime>/<agent>/.config/gh`, printed "gh auth login" and exited 4 before it asked
the keyring for a token. The tick's orchestrate.sh exports both and its children inherit them, so
the agents it starts were never affected. Over 90 days of dispatch logs, 58 of the 58 delegated
codex runs that called gh failed that way on their first call, and 25 of them then read the token
through `git credential fill` instead. cursor and gemini runs reported the same failure in their
own words.

THE FIX. The prelude exports GH_CONFIG_DIR, resolved by gh's own rule from the DISPATCHER's
environment (`dispatcher.gh_config_dir`). gh then takes the token from wherever that config says,
the keyring on the owner's machine, exactly as the dispatcher's gh does. The pin adds no token to
the agent's argv or environment, and every other XDG-honouring tool keeps the runtime redirect.

The behavioural tests run the REAL gh and bash against a scratch config that holds a string that is
not a credential. HOME is in the sandbox and the environment is built from scratch, so neither the
owner's keychain nor an inherited token can reach them. The model-catalog probe is off, so no test
touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import adapters
import capacity
import dispatcher
import exp_abcd
import paths

# The router's seats, the same set adapters.build_command builds and so the set that reaches the
# prelude. A seat added there is covered here without anyone remembering this file.
AGENTS = tuple(capacity.AGENTS)
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
    # exp_abcd._eval_command resolves gemini's model for every seat, and a cold catalog cache makes
    # that a real `agy models` server call that rewrites the cache. ORCH_MODEL_PROBE=0 is adapters'
    # own off switch, used for the same call by tests/test_experiment_arm_identity.py.
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(adapters, "_ADVERTISED_MEMO", {})
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
    # Inside a dispatched agent XDG_CONFIG_HOME names the runtime and GH_CONFIG_DIR carries the pin,
    # so an agent that dispatches in turn hands its own agent the same config only if explicit wins.
    monkeypatch.setenv("GH_CONFIG_DIR", str(sandbox / "explicit"))
    assert dispatcher.gh_config_dir() == sandbox / "explicit"


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_prelude_pins_gh_to_its_dispatchers_config(sandbox, monkeypatch, agent):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "dispatcher-xdg"))
    prelude = dispatcher._agent_runtime_prelude(agent)
    assert _pins(prelude) == [str(sandbox / "dispatcher-xdg" / "gh")], prelude


@pytest.mark.parametrize("agent", AGENTS)
def test_the_prelude_still_redirects_xdg_config_home_into_the_agents_runtime(sandbox, agent):
    """Not about gh: the pin is additive, so every other XDG-honouring tool keeps the redirect.

    Kept apart from the gh tests so that narrowing the redirect, which is a design question of its
    own, fails this test by name and not six tests named for gh.
    """
    runtime_config = sandbox / "agent-runtime" / agent / ".config"
    prelude = dispatcher._agent_runtime_prelude(agent)
    assert f"export XDG_CONFIG_HOME={shlex.quote(str(runtime_config))};" in prelude, prelude


@pytest.mark.parametrize("agent", AGENTS)
def test_the_pin_is_a_path_and_never_a_token(sandbox, monkeypatch, agent):
    """A dispatcher holding a token in its environment still writes no token into the prelude."""
    monkeypatch.setenv("GH_TOKEN", FAKE)
    monkeypatch.setenv("GITHUB_TOKEN", FAKE)
    prelude = dispatcher._agent_runtime_prelude(agent)
    assert FAKE not in prelude, prelude
    assert not dispatcher.TOKEN_ASSIGNMENT.search(prelude), prelude
    assert _pins(prelude) == [str(sandbox / "home" / ".config" / "gh")], prelude


def test_the_token_guard_refuses_an_assignment_and_allows_an_unset():
    """The guard names the one forbidden thing; `unset GH_TOKEN` is how a token would be REMOVED."""
    caught = (
        "export GH_TOKEN=abc; ",
        "GH_TOKEN=abc gh pr list; ",
        "env GITHUB_TOKEN=abc cmd; ",
        "declare -x GH_TOKEN=abc; ",
    )
    allowed = (
        "unset GH_TOKEN GITHUB_TOKEN; ",
        "env -u GH_TOKEN cmd; ",
        "export GH_CONFIG_DIR=/x; ",
    )
    assert [s for s in caught if not dispatcher.TOKEN_ASSIGNMENT.search(s)] == []
    assert [s for s in allowed if dispatcher.TOKEN_ASSIGNMENT.search(s)] == []


@pytest.mark.parametrize("agent", AGENTS)
def test_the_kill_switch_removes_the_pin_and_nothing_else(sandbox, monkeypatch, agent):
    pinned = [part for part in dispatcher._agent_runtime_prelude(agent).split("; ") if part]
    monkeypatch.setenv(dispatcher.AGENT_GH_CONFIG_DISABLED_ENV, "1")
    unpinned = [part for part in dispatcher._agent_runtime_prelude(agent).split("; ") if part]
    assert not any("GH_CONFIG_DIR" in part for part in unpinned), unpinned
    dropped = [part for part in pinned if part not in unpinned]
    assert len(dropped) == 1 and dropped[0].startswith("export GH_CONFIG_DIR="), dropped
    assert [part for part in pinned if part != dropped[0]] == unpinned


@pytest.mark.parametrize("value,disabled", [(" 1", True), ("1\r", True), ("0", False), ("", False)])
def test_the_kill_switch_reads_a_padded_one_and_only_one(sandbox, monkeypatch, value, disabled):
    """`1\\r` is what a CRLF env file sourced with `set -a` delivers, and it must still disable."""
    monkeypatch.setenv(dispatcher.AGENT_GH_CONFIG_DISABLED_ENV, value)
    assert dispatcher.agent_gh_config_disabled() is disabled
    pins = _pins(dispatcher._agent_runtime_prelude("codex"))
    assert pins == ([] if disabled else [str(sandbox / "home" / ".config" / "gh")]), pins


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


@contextmanager
def _no_registry_seeded():
    """Fail if a child seeded a registry inside MODULE_DIR, the directory the mirror deploys.

    Every child launched from this file goes through it: an unguarded launch that seeds first also
    disarms a guarded one after it, because each computes what was absent when it started.
    """
    seeded = [paths.MODULE_DIR / "experiments" / n for n in paths.SEEDED_REGISTRY_ENV.values()]
    absent = [path for path in seeded if not path.exists()]
    yield
    created = [str(path) for path in absent if path.exists()]
    assert not created, f"the child seeded a registry inside MODULE_DIR: {created}"


def _run_child(sandbox: Path, script: str, *args: str, **extra: str) -> dict:
    with _no_registry_seeded():
        proc = subprocess.run(
            [sys.executable, "-c", script, *args],
            cwd=sandbox,
            env=_child_env(sandbox, **extra),
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (proc.stdout[-3000:], proc.stderr[-3000:])
    return json.loads(lines[0][len("RESULT ") :])


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


@pytest.mark.parametrize(
    "variable,value,suffix",
    [
        ("GH_CONFIG_DIR", "config/gh", "config/gh"),
        ("XDG_CONFIG_HOME", "config", "config/gh"),
    ],
)
def test_relative_config_paths_keep_dispatcher_identity_across_child_cwd(
    sandbox, monkeypatch, variable, value, suffix
):
    parent = sandbox / "dispatcher-cwd"
    child = sandbox / "child-cwd"
    parent.mkdir()
    child.mkdir()
    monkeypatch.chdir(parent)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv(variable, value)
    prelude = dispatcher._agent_runtime_prelude("codex")
    command = "printf '%s' \"$GH_CONFIG_DIR\""
    ran = subprocess.run(
        ["bash", "-c", prelude + command],
        cwd=child,
        env=_child_env(sandbox),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stderr
    assert Path(ran.stdout) == parent / suffix


def test_dispatcher_selftest_runs_with_the_gh_config_pin_disabled(sandbox):
    with _no_registry_seeded():
        ran = subprocess.run(
            [sys.executable, str(paths.MODULE_DIR / "dispatcher.py"), "--selftest"],
            cwd=sandbox,
            env=_child_env(sandbox, ORCH_AGENT_GH_CONFIG_DISABLED="1"),
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
    assert ran.returncode == 0, (ran.stdout[-2000:], ran.stderr[-2000:])
    assert "dispatcher.py selftest: OK" in ran.stdout
