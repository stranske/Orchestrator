"""A gemini dispatch names its workspace with ONE canonical string, however the runtime is spelled.

THE DEFECT (diagnosed 2026-10-02, while fixing #394). `adapters.build_command` resolves gemini's
`--add-dir`, which is agy's write-isolation guard, and `_gemini_workspace_prompt` resolves the
GEMINI WORKSPACE line. `plan_dispatch` returned the provisioned worktree as it came. On macOS every
scratch runtime sits behind a symlink (`/var` -> `/private/var`), and mktemp keeps $TMPDIR's
trailing slash, so with `ORCH_LOCAL_RUNTIME=/var/folders/.../T//x` one dispatch named its
workspace with two strings: the spawn cwd and the claim's `worktree` said `/var/...`, the guard
said `/private/var/...`, and `dispatcher.py --selftest` failed its `--add-dir == cwd` assertion.

The isolated offload had the same shape one step removed. It writes its copy's path into
`--add-dir` over the adapter's resolved value, and the copy lives under OFFLOAD_DIR, which nothing
resolved.

Both tests build that runtime on purpose, as a symlink followed by a doubled slash, so they behave
the same on a Linux runner, where nothing under the temp root is a symlink. Each runs in a child
interpreter because every runtime path is fixed from the environment at import, which is how the
defect reached the dispatcher. The child inherits no ORCH_* variable, so no inherited override can
point it back at live state (`rail_exercise.sandbox_overrides()` names the two that matter most),
and its HOME is the sandbox. The registries that seed on a first load (`paths.SEEDED_REGISTRY_ENV`)
are pointed into the sandbox as well, because their default is MODULE_DIR, which the mirror deploys.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import paths
import provision

TARGET, LANE = "o/r#8", "opener"

PLAN = """
import json, sys
import dispatcher

d = dispatcher.plan_dispatch(
    {"agent": "gemini", "target": sys.argv[1], "task_type": "implement", "mode": "full",
     "lane": sys.argv[2], "prompt": "Summarize only."},
    dry_run=True,
)
print("RESULT " + json.dumps({"cwd": d["cwd"], "argv": d["argv"], "wrapped": d["wrapped"]}))
"""

# Only the agent invocation is faked: offload runs it as `bash -lc <wrapped>`, and every other
# subprocess (git probing the copy, say) runs for real.
OFFLOAD = """
import json, subprocess, sys
import dispatcher

seen, real_run = {}, subprocess.run

class Finished:
    returncode, stdout, stderr = 0, "OFFLOAD RESULT", ""

def run(cmd, *args, **kwargs):
    if isinstance(cmd, list) and cmd[:2] == ["bash", "-lc"]:
        seen["wrapped"] = cmd[2]
        return Finished()
    return real_run(cmd, *args, **kwargs)

subprocess.run = run
off = dispatcher.offload(
    "gemini", "Inspect the isolated copy.", cwd=sys.argv[1], isolate=True, timeout=60
)
print("RESULT " + json.dumps({"off": off, **seen}))
"""


def _runtime_behind_a_symlink(tmp_path: Path) -> tuple[Path, Path, str]:
    """`real/runtime`, the symlink to `real`, and the runtime spelled through it with a `//`."""
    real = tmp_path / "real"
    (real / "runtime").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    return real, link, f"{link}//runtime"


SEEDED = paths.SEEDED_REGISTRY_ENV
SEEDED_DEFAULTS = [paths.MODULE_DIR / "experiments" / name for name in SEEDED.values()]


def _run_child(tmp_path: Path, script: str, runtime: str, *args: str, **env_extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_") and k != "HANDOFF_DIR"}
    env.update(
        HOME=str(tmp_path / "home"),
        HANDOFF_DIR=str(tmp_path / "handoff"),
        ORCH_LOCAL_RUNTIME=runtime,
        ORCH_STATE_DIR=f"{runtime}/state",
        # adapters' documented kill-switch: pinned models, no `agy models` catalog probe.
        ORCH_MODEL_PROBE="0",
        PYTHONPATH=str(paths.MODULE_DIR),
        **env_extra,
    )
    # Scrubbing ORCH_* also drops the variables that keep a first load's SEED out of MODULE_DIR,
    # and the exec mirror deploys that directory: this child seeding repo_knowledge.json there
    # VOIDed every verified sync on 2026-10-04. The seeds belong in the sandbox too.
    env.update({var: str(tmp_path / "registries" / name) for var, name in SEEDED.items()})
    absent = [path for path in SEEDED_DEFAULTS if not path.exists()]
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    created = [str(path) for path in absent if path.exists()]
    assert (
        not created
    ), f"the child seeded a registry inside MODULE_DIR, which the mirror deploys: {created}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (
        f"child exited {proc.returncode} with {len(lines)} RESULT line(s)\n"
        f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
    )
    return json.loads(lines[0][len("RESULT ") :])


def _between(text: str, before: str, after: str) -> str:
    assert before in text, f"{before!r} is missing from:\n{text}"
    return text.split(before, 1)[1].split(after, 1)[0]


def _assert_canonical(spelling: str, link: Path) -> None:
    assert "//" not in spelling, f"doubled slash survived: {spelling}"
    assert not spelling.startswith(f"{link}/"), f"still spelled through the symlink: {spelling}"
    assert str(Path(spelling).resolve()) == spelling, f"not canonical: {spelling}"


def test_plan_dispatch_names_the_gemini_workspace_with_one_canonical_string(tmp_path):
    real, link, runtime = _runtime_behind_a_symlink(tmp_path)
    out = _run_child(tmp_path, PLAN, runtime, TARGET, LANE)
    argv = out["argv"]

    add_dir = argv[argv.index("--add-dir") + 1]
    prompt = argv[argv.index("--print") + 1]
    workspace_line = _between(prompt, "GEMINI WORKSPACE: use exactly ", " for all file reads")
    expected = str(
        (real / "runtime" / "worktrees" / provision.worktree_name(TARGET, LANE)).resolve()
    )
    strings = {"returned cwd": out["cwd"], "--add-dir": add_dir, "prompt": workspace_line}
    spellings = set(strings.values())
    assert spellings == {expected}, f"{len(spellings)} spelling(s), expected {expected}: {strings}"
    _assert_canonical(expected, link)

    # The agent-runtime dir is a different population and it already has one spelling: the
    # prelude's ORCH_AGENT_RUNTIME and the argv's --gemini_dir/--log-file all derive, unresolved,
    # from the same environment. Resolving one side alone would split it the way the workspace was
    # split, so this pins the agreement rather than a spelling.
    exported = re.search(r"export ORCH_AGENT_RUNTIME=('[^']*'|[^\s;]+);", out["wrapped"])
    assert exported, out["wrapped"]
    base = shlex.split(exported.group(1))[0]
    assert argv[argv.index("--gemini_dir") + 1] == f"{base}/.gemini", (base, argv)
    assert argv[argv.index("--log-file") + 1] == f"{base}/logs/agy.log", (base, argv)


def test_isolated_gemini_offload_names_its_copy_with_one_canonical_string(tmp_path):
    real, link, runtime = _runtime_behind_a_symlink(tmp_path)
    source = real / "source"
    source.mkdir()
    (source / "notes.txt").write_text("copied into the isolated workspace\n")
    out = _run_child(
        tmp_path, OFFLOAD, runtime, f"{link}//source", ORCH_OFFLOAD_DIR=f"{runtime}/offloads"
    )
    off = out["off"]
    assert off["exit"] == 0 and "wrapped" in out, f"the faked agent run was never reached: {off}"

    isolated = off["isolated_cwd"]
    parts = shlex.split(out["wrapped"])
    add_dir = parts[parts.index("--add-dir") + 1]
    prompt = parts[parts.index("--print") + 1]
    workspace_line = _between(prompt, "GEMINI ISOLATED WORKSPACE: use ", " as the workspace")
    strings = {"isolated_cwd": isolated, "--add-dir": add_dir, "prompt": workspace_line}
    assert len(set(strings.values())) == 1, f"one copy, several spellings: {strings}"
    _assert_canonical(isolated, link)
    assert Path(isolated).parent == (real / "runtime" / "offloads").resolve(), isolated
    assert (Path(isolated) / "notes.txt").is_file(), f"{isolated} is not the copy offload made"
