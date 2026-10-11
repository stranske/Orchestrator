"""An offload can be held to a caller's write boundary, whichever agent runs it.

THE DEFECT (measured 2026-10-11). The research program's driver offloads every unit with the
program's own code directory as cwd. From 2026-09-06 to 10-04, 232 workers ran there: cursor 144,
gemini 50, codex 36, vibe 2. codex could write it because `--cd` is a write entry of its permission
profile; cursor (`--force`), agy (`--dangerously-skip-permissions`) and vibe (`--auto-approve`) have
no sandbox at all. So 232 of 232 could rewrite `driver.py` and `program.py`, which launchd runs every
15 minutes outside any sandbox, and the briefs, queue and owner notes the next worker obeys.

THE FIX. `dispatcher.offload(write_boundary={"protect": [...], "allow": [...]})`. codex gets one
`--add-dir` per allow root and its own sandbox does the rest. Every other agent runs under a
`sandbox-exec` profile that denies writes under `protect`, re-allows the cwd and the allow roots, and
denies any `.git` beneath them. A boundary that cannot be enforced refuses the run. No boundary
changes nothing.

These tests read the argv the dispatcher would run; they never execute sandbox-exec, so they run on
any machine. The enforcement itself was demonstrated with real agents (see the PR).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import adapters
import paths

SEEDED = paths.SEEDED_REGISTRY_ENV
SEEDED_DEFAULTS = [paths.MODULE_DIR / "experiments" / name for name in SEEDED.values()]

# The fake agent records the wrapped command `offload` would run and answers like a finished run.
FAKE_AGENT = r"""
import json, os, shlex, subprocess, sys

seen, real_run = [], subprocess.run

class Finished:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""

def run(cmd, *args, **kwargs):
    if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
        seen.append({"wrapped": cmd[2], "cwd": str(kwargs.get("cwd"))})
        text = "done"
        if "--json" in shlex.split(cmd[2]):
            text = "\n".join(json.dumps(event) for event in (
                {"type": "thread.started", "thread_id": "t1"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                {"type": "turn.completed"},
            ))
        return Finished(text)
    return real_run(cmd, *args, **kwargs)

subprocess.run = run
"""

OFFLOAD = FAKE_AGENT + r"""
import adapters, dispatcher

spec = json.loads(sys.argv[1])
if spec.get("sandbox_exec"):
    adapters.SANDBOX_EXEC = spec["sandbox_exec"]
results = {}
for agent in spec["agents"]:
    seen.clear()
    off = dispatcher.offload(agent, "Write the report.", cwd=spec["cwd"], timeout=60,
                             write_boundary=spec.get("boundary"))
    results[agent] = {"seen": list(seen), "exit": off.get("exit"), "error": off.get("error")}
print("RESULT " + json.dumps(results))
"""


def _run_child(tmp_path: Path, spec: dict, extra_env: dict | None = None) -> dict:
    box = {name: tmp_path / name for name in ("home", "runtime", "handoff")}
    for path in box.values():
        path.mkdir(parents=True, exist_ok=True)
    codex_bin = tmp_path / "bin" / "codex"
    codex_bin.parent.mkdir(exist_ok=True)
    codex_bin.touch()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_") and k != "HANDOFF_DIR"}
    env.pop("CODEX_SANDBOX", None)  # a seatbelt parent switches codex to the bypass flag
    env.update(
        HOME=str(box["home"]),
        HANDOFF_DIR=str(box["handoff"]),
        ORCH_LOCAL_RUNTIME=str(box["runtime"]),
        ORCH_STATE_DIR=str(box["runtime"] / "state"),
        ORCH_CODEX_PROFILE_BIN=str(codex_bin),
        ORCH_MODEL_PROBE="0",
        PYTHONPATH=str(paths.MODULE_DIR),
    )
    env.update({var: str(tmp_path / "registries" / name) for var, name in SEEDED.items()})
    env.update(extra_env or {})
    absent = [path for path in SEEDED_DEFAULTS if not path.exists()]
    proc = subprocess.run(
        [sys.executable, "-c", OFFLOAD, json.dumps(spec)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    created = [str(path) for path in absent if path.exists()]
    assert not created, f"the child seeded a registry inside MODULE_DIR: {created}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (
        f"child exited {proc.returncode}\nstdout:\n{proc.stdout[-3000:]}\n"
        f"stderr:\n{proc.stderr[-3000:]}"
    )
    return json.loads(lines[0][len("RESULT ") :])


AGENT_STARTS = {"agy": "--gemini_dir", "cursor-agent": "-p", "vibe": "--prompt"}


def _agent_argv(wrapped: str) -> list[str]:
    """The agent's own argv: the wrapper's last command, after any sandbox-exec prefix."""
    parts = shlex.split(wrapped)
    for start in range(len(parts) - 2, -1, -1):
        token, after = parts[start], parts[start + 1]
        codex = (token == "codex" or token.endswith("/codex")) and after == "exec"
        if codex or AGENT_STARTS.get(token) == after:
            return parts[start:]
    raise AssertionError(f"no agent invocation in {wrapped[-600:]}")


def _program(tmp_path: Path) -> dict[str, Path]:
    prog = (tmp_path / "program").resolve()
    places = {
        "prog": prog,
        "unit": prog / "unitdirs" / "u1",
        "out": prog / "artifacts" / "audits",
        "clone": prog / "clones" / "Repo",
    }
    for path in places.values():
        path.mkdir(parents=True, exist_ok=True)
    return places


def _spec(tmp_path: Path, agents: list[str], **extra) -> tuple[dict, dict[str, Path]]:
    p = _program(tmp_path)
    spec = {
        "agents": agents,
        "cwd": str(p["unit"]),
        "boundary": {"protect": [str(p["prog"])], "allow": [str(p["out"]), str(p["clone"])]},
        # Any existing file stands in for sandbox-exec: the argv is read, never executed.
        "sandbox_exec": sys.executable,
    }
    spec.update(extra)
    return spec, p


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


def test_codex_gets_exactly_its_roots_and_no_seatbelt(tmp_path):
    spec, p = _spec(tmp_path, ["codex"])
    out = _run_child(tmp_path, spec)["codex"]
    assert out["exit"] == 0, out
    wrapped = out["seen"][0]["wrapped"]
    assert sys.executable + " -p" not in wrapped, "codex is held by its own sandbox, not a second"
    argv = _agent_argv(wrapped)
    assert argv[argv.index("--cd") + 1] == str(p["unit"])
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert _add_dirs(argv) == [str(p["out"]), str(p["clone"])]
    assert "WRITE BOUNDARY" in argv[-1]


@pytest.mark.parametrize("agent", ["cursor", "gemini", "vibe"])
def test_an_agent_without_a_sandbox_runs_under_the_profile(tmp_path, agent):
    spec, p = _spec(tmp_path, [agent])
    out = _run_child(tmp_path, spec)[agent]
    assert out["exit"] == 0, out
    wrapped = out["seen"][0]["wrapped"]
    parts = shlex.split(wrapped)
    at = parts.index(sys.executable)
    assert parts[at + 1] == "-p", parts[at : at + 3]
    profile = parts[at + 2]
    deny = f'(deny file-write* (subpath "{p["prog"]}"))'
    allows = [
        f'(allow file-write* (subpath "{root}"))' for root in (p["unit"], p["out"], p["clone"])
    ]
    assert deny in profile and all(a in profile for a in allows), profile
    # Seatbelt applies the LAST matching rule: deny, then the grants, then `.git` beneath them.
    assert profile.index(deny) < min(profile.index(a) for a in allows)
    assert profile.rindex("\\.git") > max(profile.index(a) for a in allows)
    agent_argv = parts[at + 3 :]
    if agent in ("cursor", "gemini"):
        # Their file tools are confined to a workspace, so the grants join it.
        assert [str(p["out"]), str(p["clone"])] == _add_dirs(agent_argv)[-2:]
    else:
        assert _add_dirs(agent_argv) == []
    if agent == "gemini":
        assert _add_dirs(agent_argv)[0] == str(p["unit"]), "agy's workspace stays the run's cwd"


def test_no_boundary_changes_nothing(tmp_path):
    spec, p = _spec(tmp_path, ["codex", "cursor", "gemini", "vibe"])
    spec.pop("boundary")
    out = _run_child(tmp_path, spec)
    for agent, res in out.items():
        assert res["exit"] == 0, res
        wrapped = res["seen"][0]["wrapped"]
        assert sys.executable + " -p" not in wrapped and "WRITE BOUNDARY" not in wrapped, agent
        argv = _agent_argv(wrapped)
        if agent == "codex":
            assert _add_dirs(argv) == []
        if agent == "gemini":
            assert _add_dirs(argv) == [str(p["unit"])]


@pytest.mark.parametrize(
    "case",
    ["empty_protect", "home_granted", "codex_bypassed", "no_sandbox_exec", "nested_seatbelt"],
)
def test_a_boundary_that_cannot_hold_refuses_the_run(tmp_path, case):
    agent = "codex" if case == "codex_bypassed" else "cursor"
    spec, p = _spec(tmp_path, [agent])
    env = {}
    if case == "empty_protect":
        spec["boundary"]["protect"] = []
    elif case == "home_granted":
        spec["boundary"]["allow"].append(str(tmp_path / "home"))
    elif case == "codex_bypassed":
        env["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "1"
    elif case == "no_sandbox_exec":
        spec["sandbox_exec"] = str(tmp_path / "absent" / "sandbox-exec")
    elif case == "nested_seatbelt":
        env["CODEX_SANDBOX"] = "seatbelt"
    out = _run_child(tmp_path, spec, env)[agent]
    assert out["exit"] == 2 and out["seen"] == [], out
    assert "refusing" in (out["error"] or ""), out


def test_roots_resolve_and_lead_with_the_cwd(tmp_path):
    p = _program(tmp_path)
    link = tmp_path / "link-to-out"
    link.symlink_to(p["out"])
    protect, allow = adapters.write_boundary_roots(
        [p["prog"], p["prog"]], [link, p["out"], p["clone"]], p["unit"], agent="vibe"
    )
    assert protect == [str(p["prog"])]
    assert allow == [str(p["unit"]), str(p["out"]), str(p["clone"])]


def test_the_profile_quotes_a_path_it_cannot_otherwise_name():
    profile = adapters.seatbelt_write_profile(['/x/a"b'], ['/x/a"b/c\\d'])
    assert '(deny file-write* (subpath "/x/a\\"b"))' in profile
    assert '(allow file-write* (subpath "/x/a\\"b/c\\\\d"))' in profile
