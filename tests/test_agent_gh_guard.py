"""A dispatched agent's gh refuses to change the owner's gh login or config. Owner decision 2026-10-05.

WHY. The GH_CONFIG_DIR pin (#461) gives an agent its dispatcher's gh, both ways: an unsandboxed
agent's `gh auth logout` deletes the owner's keychain token, and `gh config set` rewrites the
owner's config, for the whole machine. The guard refuses those commands and passes every other.

WHY THREE LAYERS. Measured 2026-10-05 with one real run per agent: a wrapper first on PATH reached
claude, agy and vibe, but not cursor, whose shell re-applies the login PATH, nor codex, which runs
every command with `zsh -lc`. On the owner's machine ~/.zprofile and ~/.bash_profile put
/opt/homebrew/bin back in front. So the same check is also a shell FUNCTION (it outranks PATH):
bash reads it from BASH_ENV, zsh from a proxy ZDOTDIR that first runs the owner's own dotfiles.

These tests never reach the real gh. The "real" gh is a script that records its arguments, and the
fake HOME's dotfiles re-prepend it to PATH exactly as `brew shellenv` re-prepends Homebrew.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import pytest

import dispatcher

BASH = shutil.which("bash")
ZSH = shutil.which("zsh")
NEEDS_ZSH = pytest.mark.skipif(
    ZSH is None, reason="zsh is not installed, so its startup files cannot run"
)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "REAL_HOME", tmp_path / "home")
    for var in (
        "ZDOTDIR",
        "BASH_ENV",
        "ORCH_GH_GUARD_REAL_ZDOTDIR",
        "ORCH_GH_GUARD_PREV_BASH_ENV",
        dispatcher.AGENT_GH_GUARD_DISABLED_ENV,
        dispatcher.AGENT_GH_CONFIG_DISABLED_ENV,
        "GH_CONFIG_DIR",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    real = tmp_path / "realgh"
    real.mkdir()
    log = tmp_path / "gh.log"
    (real / "gh").write_text('#!/bin/sh\nprintf \'%s\\n\' "REAL-GH $*" >> "$GH_LOG"\n')
    (real / "gh").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    reprepend = f'export PATH={shlex.quote(str(real))}:"$PATH"\n'
    (home / ".zprofile").write_text(reprepend + "export ORCH_TEST_ZPROFILE=ran\n")
    (home / ".bash_profile").write_text(reprepend + "export ORCH_TEST_BASH_PROFILE=ran\n")
    env = {"PATH": f"{real}:/usr/bin:/bin", "HOME": str(home), "GH_LOG": str(log)}
    return {"real": real, "log": log, "home": home, "env": env, "tmp": tmp_path}


def _agent(world, command: str, prelude: str | None = None) -> subprocess.CompletedProcess:
    """Run `command` the way an agent does: after the dispatcher's prelude, in a child process."""
    prelude = dispatcher._agent_runtime_prelude("codex") if prelude is None else prelude
    return subprocess.run(
        [BASH or "bash", "-c", prelude + command],
        env=world["env"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _logged(world) -> str:
    return world["log"].read_text() if world["log"].exists() else ""


SHELLS = [
    pytest.param("/bin/sh -c", id="sh-c"),
    pytest.param("bash -c", id="bash-c"),
    pytest.param("bash -lc", id="bash-lc"),
    pytest.param("zsh -c", id="zsh-c", marks=NEEDS_ZSH),
    pytest.param("zsh -lc", id="zsh-lc", marks=NEEDS_ZSH),
    pytest.param("exec", id="direct-exec"),
]


def _in(shell: str, gh_args: str) -> str:
    if shell == "exec":
        return f"exec gh {gh_args}"
    return f"exec {shell} {shlex.quote('gh ' + gh_args)}"


@pytest.mark.parametrize("shell", SHELLS)
def test_a_login_or_config_change_is_refused_in_every_shell(world, shell):
    ran = _agent(world, _in(shell, "auth logout"))
    assert ran.returncode == dispatcher.GH_GUARD_EXIT, (ran.returncode, ran.stderr)
    assert "orchestrator gh guard" in ran.stderr, ran.stderr
    assert _logged(world) == "", "the real gh ran"


@pytest.mark.parametrize("shell", SHELLS)
def test_every_other_gh_command_reaches_the_real_gh(world, shell):
    ran = _agent(world, _in(shell, "pr view 1"))
    assert ran.returncode == 0, (ran.returncode, ran.stderr)
    assert _logged(world) == "REAL-GH pr view 1\n"


# The owner's decision, written out here rather than read from the code under test, so that
# dropping a command from GH_GUARD_REFUSED fails a test instead of silently dropping its case.
DECIDED = (
    "auth logout",
    "auth login",
    "auth switch",
    "auth refresh",
    "config set",
    "alias set",
    "alias delete",
    "alias import",
)


def test_the_refused_set_is_exactly_the_decided_one():
    assert tuple(dispatcher.GH_GUARD_REFUSED) == DECIDED


@pytest.mark.parametrize("pair", DECIDED)
def test_the_wrapper_refuses_each_decided_command(world, pair):
    ran = _agent(world, f"exec gh {pair} --hostname github.com")
    assert ran.returncode == dispatcher.GH_GUARD_EXIT, (pair, ran.stderr)
    assert _logged(world) == ""


@pytest.mark.parametrize("args", ["auth status", "auth token", "config get pager", "alias list"])
def test_reads_of_the_same_groups_pass(world, args):
    ran = _agent(world, f"exec gh {args}")
    assert ran.returncode == 0 and _logged(world) == f"REAL-GH {args}\n", ran.stderr


@pytest.mark.parametrize(
    "shell,marker",
    [
        pytest.param("bash -lc", "ORCH_TEST_BASH_PROFILE", id="bash"),
        pytest.param("zsh -lc", "ORCH_TEST_ZPROFILE", id="zsh", marks=NEEDS_ZSH),
    ],
)
def test_the_owners_own_login_files_still_run(world, shell, marker):
    ran = _agent(world, f"exec {shell} {shlex.quote(f'printf %s ${marker}')}")
    assert ran.stdout == "ran", (ran.stdout, ran.stderr)


def test_the_kill_switch_removes_the_guard_and_nothing_else(world, monkeypatch):
    guarded = dispatcher._agent_runtime_prelude("codex").split("; ")
    monkeypatch.setenv(dispatcher.AGENT_GH_GUARD_DISABLED_ENV, " 1")
    plain = dispatcher._agent_runtime_prelude("codex")
    assert "ZDOTDIR" not in plain and "BASH_ENV" not in plain and "gh-guard" not in plain, plain
    assert [p for p in guarded if p in plain.split("; ")] == plain.split("; ")
    ran = _agent(world, "exec gh auth logout", prelude=plain)
    assert ran.returncode == 0 and _logged(world) == "REAL-GH auth logout\n"


def test_a_dispatcher_inside_a_guarded_agent_keeps_the_owners_originals(world, monkeypatch):
    """Nested dispatch must not make the proxy source itself, or chain BASH_ENV to itself."""
    guard = dispatcher.AGENT_RUNTIME_DIR / "gh-guard"
    monkeypatch.setenv("ZDOTDIR", str(guard / "zdotdir"))
    monkeypatch.setenv("ORCH_GH_GUARD_REAL_ZDOTDIR", "/owner/zdotdir")
    monkeypatch.setenv("BASH_ENV", str(guard / "bashenv.sh"))
    monkeypatch.setenv("ORCH_GH_GUARD_PREV_BASH_ENV", "/owner/bashenv")
    prelude = dispatcher._agent_runtime_prelude("codex")
    assert "export ORCH_GH_GUARD_REAL_ZDOTDIR=/owner/zdotdir;" in prelude, prelude
    assert "export ORCH_GH_GUARD_PREV_BASH_ENV=/owner/bashenv;" in prelude, prelude


def test_an_inherited_bash_env_still_runs_under_the_guard(world, monkeypatch):
    owners = world["tmp"] / "owners_bashenv.sh"
    owners.write_text("export ORCH_TEST_OWNER_BASH_ENV=ran\n")
    monkeypatch.setenv("BASH_ENV", str(owners))
    ran = _agent(world, "exec bash -c 'printf %s \"$ORCH_TEST_OWNER_BASH_ENV\"; gh auth logout'")
    assert ran.stdout == "ran" and ran.returncode == dispatcher.GH_GUARD_EXIT, (
        ran.stdout,
        ran.stderr,
    )


def test_guard_files_are_rewritten_only_when_they_differ(world):
    guard = dispatcher._ensure_gh_guard()
    wrapper = guard / "bin" / "gh"
    first = wrapper.stat().st_mtime_ns
    os.utime(wrapper, ns=(first - 10**9, first - 10**9))
    dispatcher._ensure_gh_guard()
    assert wrapper.stat().st_mtime_ns == first - 10**9, "an unchanged guard file was rewritten"
    assert os.access(wrapper, os.X_OK)


@pytest.mark.parametrize(
    "args",
    [
        "-R o/r auth logout",
        "auth --hostname github.com logout",
        "--repo o/r config set git_protocol ssh",
        "alias --clobber set co 'pr checkout'",
    ],
)
def test_options_before_or_inside_the_command_do_not_get_it_past_the_guard(world, args):
    for command in (f"exec gh {args}", f"exec bash -lc {shlex.quote('gh ' + args)}"):
        ran = _agent(world, command)
        assert ran.returncode == dispatcher.GH_GUARD_EXIT, (command, ran.stderr)
    assert _logged(world) == ""


def test_a_failed_guard_write_leaves_no_temp_file(world, monkeypatch):
    def boom(*_args):
        raise OSError("simulated")

    monkeypatch.setattr(dispatcher.os, "replace", boom)
    with pytest.raises(OSError):
        dispatcher._ensure_gh_guard()
    leftovers = list((dispatcher.AGENT_RUNTIME_DIR / "gh-guard").rglob("*.tmp"))
    assert leftovers == [], leftovers
