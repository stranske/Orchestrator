"""No agent run gets a workspace that is, or holds, `/`, the home dir or the control plane.

THE DEFECT (measured 2026-10-05). An agent's workspace is where it may write. codex turns `--cd`
into a write entry of the run's own permission profile, so `writable_roots` in ~/.codex/config.toml
cannot narrow it; agy's `--add-dir` is agy's write-isolation guard; cursor, claude and vibe have no
sandbox and work in the process cwd. The tick runs from launchd with cwd `/`, and every role offloads
with `cwd="."` unless it has a worktree, so `adapters.workspace_path(".")` was `/`. The capacity
ledger shows 1,521 offloads in `/` from 2026-07-10 to 2026-10-02 (gemini 1,279, cursor 224, codex 18),
1,513 of them redirect or triage role runs, and a real `codex exec` from `/` could write
`~/.codex/config.toml`, `~/.codex/bin` and `~/.claude`.

THE FIX. `adapters.broad_workspace_reason` is the one predicate. `dispatcher.offload` replaces a
refused workspace with a fresh per-run scratch directory under `$ORCH_STATE_DIR` before anything
else sees it (`adapters.agent_workspace`), so the role still runs. `plan_dispatch` skips a refused
provisioned worktree like a provision failure, because a committing run cannot work in scratch. And
`build_command` refuses to emit codex `--cd` (unless read-only) or agy `--add-dir` for a refused
workspace, whoever calls it.

The role tests run in a child interpreter whose process cwd IS `/`, because that is the defect's
precondition and every runtime path is fixed from the environment at import. The child inherits no
ORCH_* variable, its HOME is the sandbox, and the registries that seed on a first load point into the
sandbox (the pattern of test_gemini_workspace_one_spelling.py). Only the agent invocation is faked:
`offload` runs it as `bash -lc <wrapped>`, and the fake records that string and the cwd it was given.
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

# The fake agent answers with a valid RedirectAgent proposal, as a codex `exec --json` stream for
# codex and as plain text for everyone else, so the role can show it still works after relocation.
FAKE_AGENT = r"""
import json, os, shlex, subprocess, sys

PROPOSAL = {"action": "wait", "reason": "the agent is still working", "confidence": "medium",
            "corrected_prompt": "", "switch_agent": None}
seen, real_run = [], subprocess.run

class Finished:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""

def run(cmd, *args, **kwargs):
    if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
        seen.append({"wrapped": cmd[2], "cwd": str(kwargs.get("cwd"))})
        text = json.dumps(PROPOSAL)
        if "--json" in shlex.split(cmd[2]):  # only codex asks for its `exec --json` stream
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

ROLE = FAKE_AGENT + r"""
import roles

role, backend = sys.argv[1], sys.argv[2]
if role == "redirect":
    # A report with no worktree: the role falls back to cwd ".", which is the tick's `/`.
    report = {"agent": "cursor", "target": "o/r#7", "lane": "opener", "task_type": "implement",
              "state": "stalled", "recommended_action": "inspect", "log_tail": "still running\n"}
    out = roles.run_redirect_agent(report, "Tests pass.", backend=backend, dispatch=True,
                                   learned={}, exploration_rate=0.0, timeout=60)
    summary = {"decision_source": out.get("decision_source"), "errors": out.get("errors")}
else:
    items = [{"target": "o/r#1", "title": "Fix the flaky test", "labels": []}]
    out = roles.run_triage_agent(backlog_items=items, backend=backend, dispatch=True,
                                 learned={}, exploration_rate=0.0, timeout=60)
    summary = {"decision_source": out.get("decision_source")}
print("RESULT " + json.dumps({"process_cwd": os.getcwd(), "seen": seen, **summary}))
"""

OFFLOAD_EACH = FAKE_AGENT + r"""
import dispatcher

results = {}
for agent in sys.argv[2].split(","):
    seen.clear()
    off = dispatcher.offload(agent, "Summarize.", cwd=sys.argv[1], timeout=60)
    results[agent] = {"seen": list(seen), "relocated": off.get("workspace_relocated"),
                      "exit": off.get("exit"), "error": off.get("error"), "log": off.get("log")}
print("RESULT " + json.dumps({"process_cwd": os.getcwd(), "results": results}))
"""

PLAN = r"""
import json, sys
from pathlib import Path
import dispatcher, provision

provision.worktree_path = lambda target, lane: Path(sys.argv[1])
d = dispatcher.plan_dispatch(
    {"agent": sys.argv[2], "target": "o/r#8", "task_type": "implement", "mode": "full",
     "lane": "opener", "prompt": "Implement it."},
    dry_run=True,
)
print("RESULT " + json.dumps(d if d is None or "error" in d else {"cwd": d["cwd"], "argv": d["argv"]}))
"""


def _sandbox(tmp_path: Path) -> dict[str, Path]:
    places = {
        "home": tmp_path / "home",
        "runtime": tmp_path / "runtime",
        "state": tmp_path / "runtime" / "state",
        "handoff": tmp_path / "handoff",
        "work": tmp_path / "work",
    }
    for path in places.values():
        path.mkdir(parents=True, exist_ok=True)
    return places


def _run_child(tmp_path: Path, script: str, *args: str, cwd: str | Path = "/") -> dict:
    box = _sandbox(tmp_path)
    # An exact codex profile fails closed without a version-capable binary, which a bare runner
    # lacks. The agent call is faked, so a placeholder file is all the profile path needs.
    codex_bin = tmp_path / "bin" / "codex"
    codex_bin.parent.mkdir(exist_ok=True)
    codex_bin.touch()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_") and k != "HANDOFF_DIR"}
    env.pop("CODEX_SANDBOX", None)  # a seatbelt parent would switch codex to the bypass flag
    env.update(
        HOME=str(box["home"]),
        HANDOFF_DIR=str(box["handoff"]),
        ORCH_LOCAL_RUNTIME=str(box["runtime"]),
        ORCH_STATE_DIR=str(box["state"]),
        ORCH_CODEX_PROFILE_BIN=str(codex_bin),
        # adapters' documented kill-switch: pinned models, no `agy models` catalog probe.
        ORCH_MODEL_PROBE="0",
        PYTHONPATH=str(paths.MODULE_DIR),
    )
    env.update({var: str(tmp_path / "registries" / name) for var, name in SEEDED.items()})
    absent = [path for path in SEEDED_DEFAULTS if not path.exists()]
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=str(cwd),
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
        f"child exited {proc.returncode} with {len(lines)} RESULT line(s)\n"
        f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
    )
    result = json.loads(lines[0][len("RESULT ") :])
    result["stderr"] = proc.stderr
    return result


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


# Each agent CLI and the argument `adapters.build_command` always puts right after it.
AGENT_STARTS = {"agy": "--gemini_dir", "cursor-agent": "-p", "claude": "-p", "vibe": "--prompt"}


def _agent_argv(wrapped: str) -> list[str]:
    """The agent's own argv: the wrapper's last command, which is `shlex.join(argv)`."""
    parts = shlex.split(wrapped)
    for start in range(len(parts) - 2, -1, -1):
        token, after = parts[start], parts[start + 1]
        codex = (token == "codex" or token.endswith("/codex")) and after == "exec"
        if codex or AGENT_STARTS.get(token) == after:
            return parts[start:]
    raise AssertionError(f"no agent invocation in {wrapped[-600:]}")


def _assert_scratch(path: str, tmp_path: Path) -> None:
    scratch_root = (tmp_path / "runtime" / "state" / "scratch-workspaces").resolve()
    assert Path(path).parent == scratch_root, f"{path} is not a scratch dir under {scratch_root}"
    assert adapters.broad_workspace_reason(path) is None, path


@pytest.mark.parametrize(
    "role,backend",
    [("redirect", "codex"), ("redirect", "gemini"), ("redirect", "cursor"), ("triage", "gemini")],
)
def test_a_role_offload_from_root_never_gets_root_as_its_workspace(tmp_path, role, backend):
    out = _run_child(tmp_path, ROLE, role, backend)
    assert out["process_cwd"] == "/", "the precondition: the role runs with the tick's cwd"
    assert len(out["seen"]) == 1, f"the backend was not reached exactly once: {out}"
    seen = out["seen"][0]
    argv = _agent_argv(seen["wrapped"])
    workspace = seen["cwd"]
    _assert_scratch(workspace, tmp_path)
    if backend == "codex":
        assert _flag(argv, "--cd") == workspace, argv
        assert _flag(argv, "--sandbox") == "workspace-write", argv
    if backend == "gemini":
        assert _flag(argv, "--add-dir") == workspace, argv
    assert "/" not in {_flag(argv, "--cd"), _flag(argv, "--add-dir")}, argv
    assert "workspace relocated: / is the filesystem root" in out["stderr"], out["stderr"][-800:]
    if role == "redirect":
        # Fail toward motion: the role kept working, its proposal parsed and was accepted.
        assert out["decision_source"] == "redirect_agent", out


def test_every_agent_offloaded_from_root_runs_in_its_own_scratch_dir(tmp_path):
    agents = ["codex", "gemini", "cursor", "claude", "vibe"]
    out = _run_child(tmp_path, OFFLOAD_EACH, ".", ",".join(agents))
    assert out["process_cwd"] == "/"
    workspaces = set()
    for agent in agents:
        res = out["results"][agent]
        assert len(res["seen"]) == 1, (agent, res)
        relocated, seen = res["relocated"], res["seen"][0]
        assert relocated and relocated["requested"] == "/", (agent, res)
        assert seen["cwd"] == relocated["workspace"], (agent, seen, relocated)
        _assert_scratch(seen["cwd"], tmp_path)
        argv = _agent_argv(seen["wrapped"])
        assert "/" not in {_flag(argv, "--cd"), _flag(argv, "--add-dir")}, (agent, argv)
        log = Path(res["log"]).read_text()
        assert "WORKSPACE RELOCATED: / is the filesystem root" in log, (agent, log[-800:])
        assert relocated["workspace"] in seen["wrapped"], "the agent is told where it runs"
        workspaces.add(seen["cwd"])
    assert len(workspaces) == len(agents), f"runs shared a scratch dir: {workspaces}"
    ledger = tmp_path / "handoff" / "capacity-ledger.ndjson"
    starts = [json.loads(ln) for ln in ledger.read_text().splitlines()]
    starts = [row for row in starts if row.get("event") == "start"]
    assert [row.get("workspace_relocated_from") for row in starts] == ["/"] * len(agents), starts


def test_home_and_its_ancestor_are_relocated_and_an_ordinary_dir_is_not(tmp_path):
    box = _sandbox(tmp_path)
    for cwd, verb in ((box["home"], "is the user's home"), (tmp_path, "contains the user's home")):
        out = _run_child(tmp_path, OFFLOAD_EACH, str(cwd), "codex", cwd=box["work"])
        res = out["results"]["codex"]
        assert res["relocated"] and verb in res["relocated"]["reason"], (cwd, res)
        _assert_scratch(res["seen"][0]["cwd"], tmp_path)
    out = _run_child(tmp_path, OFFLOAD_EACH, str(box["work"]), "codex,gemini", cwd=box["work"])
    for agent in ("codex", "gemini"):
        res = out["results"][agent]
        assert res["relocated"] is None, (agent, res)
        assert res["seen"][0]["cwd"] == str(box["work"].resolve()), (agent, res)
        argv = _agent_argv(res["seen"][0]["wrapped"])
        assert str(box["work"].resolve()) in {_flag(argv, "--cd"), _flag(argv, "--add-dir")}, argv


@pytest.mark.parametrize("agent", ["codex", "gemini", "cursor"])
def test_plan_dispatch_skips_a_provisioned_workspace_that_is_root(tmp_path, agent):
    out = _run_child(tmp_path, PLAN, "/", agent, cwd=tmp_path)
    assert out and "error" in out, f"a committing run was planned in /: {out}"
    assert "provisioned workspace refused: / is the filesystem root" in out["error"], out
    ok = _run_child(tmp_path, PLAN, str(tmp_path / "work"), agent, cwd=tmp_path)
    assert ok and "error" not in ok and ok["cwd"] == str((tmp_path / "work").resolve()), ok


def test_build_command_never_grants_a_refused_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    for key in (
        "ORCH_LOCAL_RUNTIME",
        "ORCH_MIRROR",
        "CODEX_SANDBOX",
        "ORCH_CODEX_BYPASS_INNER_SANDBOX",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "home").mkdir()
    for agent, mode in (("codex", None), ("codex", "full"), ("gemini", None)):
        for broad in ("/", tmp_path / "home", tmp_path):
            with pytest.raises(adapters.WorkspaceRefused):
                adapters.build_command(agent, "x", mode=mode, cwd=broad)
    # A read-only codex sandbox writes nowhere, so it may still name `/`.
    assess = adapters.build_command("codex", "x", mode="assess", cwd="/")
    assert _flag(assess, "--cd") == "/" and _flag(assess, "--sandbox") == "read-only", assess
    # A narrowed profile is read-only too; the bypass is writable, so it refuses.
    narrowed = adapters.build_command("codex", "x", cwd="/", permission_mode="read-only")
    assert _flag(narrowed, "--sandbox") == "read-only", narrowed
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    with pytest.raises(adapters.WorkspaceRefused):
        adapters.build_command("codex", "x", cwd="/")


def test_a_case_variant_of_home_is_still_home(tmp_path, monkeypatch):
    """On a case-insensitive volume `/USERS/x` is `/Users/x`, and `resolve()` keeps the case it was
    given, so only the directory's identity can tell. On a case-sensitive volume the variant is a
    different, absent directory, and it must not be refused either way."""
    home = tmp_path / "Home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    for key in ("ORCH_LOCAL_RUNTIME", "ORCH_MIRROR"):
        monkeypatch.delenv(key, raising=False)
    variant = tmp_path / "HOME"
    reason = adapters.broad_workspace_reason(variant)
    if variant.exists():
        assert reason and "is the user's home" in reason, reason
    else:
        assert reason is None, reason
