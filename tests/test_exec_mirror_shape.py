"""The exec mirror is recognised by what it still IS after the sync starts shipping `.github/`.

THE INCIDENT (2026-10-02). PR #352 added `tests/test_gate_commit_status_fork_tolerance.py`, which
opens `.github/workflows/pr-00-gate.yml` with no env_prereq guard. The exec mirror had no `.github/`,
so every mirror `verify.py` after it was red with 19 `FileNotFoundError`s while CI and every checkout
were green. The repair ships `.github/` (with `docs/`, `.gitignore` and `ruff.toml`) in
`orch-sync-mirror.sh`. That broke the detector that tells the mirror apart: `exec_mirror_shape()`
required "no `.github/`" AND "not a git repository", so the shipped mirror would have read as a
CHECKOUT, taken the runner's skip ceiling, and printed `tree: checkout`. A mark that stops being true
of the tree it names is a silent mislabel, not a crash.

The marks are now "the modules sit flat at the root" (the shape the sync builds on purpose, by
`paths.checkout_root`'s own rule) AND "not a git repository" (the absence that still produces the
mirror's only mirror-only skips). env_prereq's selftest pins the AND with stubs; this pins the MARKS
themselves against real directory shapes, which is where the defect lived. Each case copies the two
real modules into a synthetic tree and asks the real function in a fresh interpreter, so no
monkeypatching stands between the assertion and the filesystem. Deterministic: no timing anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import env_prereq
import paths

_ASK = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import env_prereq\n"
    "print(json.dumps(env_prereq.exec_mirror_shape()))\n"
)


def _shape_of(tmp_path: Path, *, flat: bool, has_github: bool, is_repo: bool) -> str | None:
    tree = tmp_path / "tree"
    module_dir = tree if flat else tree / "src"
    module_dir.mkdir(parents=True)
    for module in (env_prereq, paths):
        assert module.__file__, f"{module.__name__} has no source file to copy"
        source = Path(module.__file__)
        shutil.copy2(source, module_dir / source.name)
    if has_github:
        (tree / ".github" / "workflows").mkdir(parents=True)
        (tree / ".github" / "workflows" / "pr-00-gate.yml").write_text("name: Gate\n")
    if is_repo:
        subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # git must never discover a repository ABOVE the synthetic tree and answer for it.
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", _ASK, str(module_dir)],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("flat", "has_github", "is_repo", "want_mirror"),
    [
        pytest.param(True, True, False, True, id="flat-github-no-git-is-the-shipped-mirror"),
        pytest.param(True, False, False, True, id="flat-no-github-no-git-is-the-mirror-pre-sync"),
        pytest.param(False, True, False, False, id="src-layout-no-git-is-an-archive-export"),
        pytest.param(False, False, False, False, id="src-layout-bare-no-git-is-not-the-mirror"),
        pytest.param(True, True, True, False, id="flat-git-repo-is-a-pre-move-checkout"),
        pytest.param(False, True, True, False, id="src-layout-git-repo-is-a-checkout"),
    ],
)
def test_the_mirror_is_the_flat_copy_that_is_not_a_repository(
    tmp_path, flat, has_github, is_repo, want_mirror
):
    if shutil.which("git") is None:
        pytest.skip(env_prereq.git_repo_absent() or "git is not on PATH")
    shape = _shape_of(tmp_path, flat=flat, has_github=has_github, is_repo=is_repo)
    tree = f"flat={flat} .github={has_github} git-repo={is_repo}"
    if want_mirror:
        assert shape, f"{tree} is the exec mirror's shape and read as a checkout"
        assert "src/" in shape and "git" in shape, f"the reason must name both marks: {shape}"
    else:
        assert shape is None, f"{tree} is a checkout's shape and read as the mirror: {shape}"


# --------------------------------------------------------------------------- WHICH MACHINE
# `bare_machine()` splits the exec-mirror tree into two agreements: the owner's provisioned mirror
# (`mirror_*`) and CI's flat copy on a runner (`bare_mirror_*`). Asked of the REAL function in a fresh
# interpreter with PATH, HOME and the Codex binary path controlled, so the answer comes from the
# filesystem and no stub stands between them. PATH holds only /usr/bin and /bin, which carry none of
# the seat CLIs on either macOS or a runner; the interpreter is invoked by absolute path.

_ASK_MACHINE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import env_prereq\n"
    "print(json.dumps(env_prereq.bare_machine()))\n"
)


def _machine(tmp_path: Path, *, cli: str | None = None, skill=False, codex=False) -> str | None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if cli:
        (bin_dir / cli).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / cli).chmod(0o755)
    if skill:
        resource = home / ".codex/skills/code-workspace-hygiene/scripts/audit_code_root.sh"
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_text("#!/bin/sh\n")
    codex_bin = tmp_path / "codex-profile-bin"
    if codex:
        codex_bin.write_text("#!/bin/sh\n")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "ORCH_CODEX_PROFILE_BIN": str(codex_bin),
        "ORCH_LOCAL_RUNTIME": str(tmp_path / "runtime"),
        "ORCH_STATE_DIR": str(tmp_path / "state"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _ASK_MACHINE, str(paths.MODULE_DIR)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_machine_with_no_local_prerequisite_is_bare(tmp_path: Path) -> None:
    got = _machine(tmp_path)
    assert got and "none of this instance's local prerequisites" in got, got


@pytest.mark.parametrize(
    "present",
    [
        pytest.param({"cli": "claude"}, id="one-seat-cli-present"),
        pytest.param({"skill": True}, id="reference-skill-present"),
        pytest.param({"codex": True}, id="codex-binary-present"),
    ],
)
def test_any_one_local_prerequisite_keeps_the_machine_provisioned(
    tmp_path: Path, present: dict
) -> None:
    """One mark present is enough: the machine then takes the smaller `mirror_*` agreement."""
    assert _machine(tmp_path, **present) is None
