"""`scripts/build_exec_mirror.sh` is the ONE definition of what the flat exec mirror carries.

THE INCIDENT (2026-10-04). The owner's verified sync of main was refused for three defects that
existed only in the flat mirror (#408), each green in CI because CI verified a CHECKOUT. The whole
copy contract lived in `~/.codex/bin/orch-sync-mirror.sh`, a file on one machine that no CI job can
read. It now lives in the repository: CI's `exec-mirror` job builds its flat copy with this script,
and the owner's copier calls it (docs/MIRROR_SYNC_PATCH.md, "One copy contract").

So these tests pin the contract's consumers to the builder, not to a list of files. The installer
must own everything the builder ships, the pre-sync identity must cover everything it reads from the
working tree, and the documented copier must delegate to it. Each is asserted from BEHAVIOUR: the
builder is run, and its output is compared. A second list in a test would be one more copy of the
contract free to drift. Every source here is a synthetic git repository in tmp_path, so the tests
need `git` and nothing else, and they run in every tree shape, flat or not, with no new skips.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from scripts import install_verified_snapshot as installer

import paths
import verify

ROOT = paths.REPO_ROOT
BUILDER = ROOT / "scripts" / "build_exec_mirror.sh"
INSTALLER = ROOT / "scripts" / "install_verified_snapshot.py"
GUARD = ROOT / "scripts" / "incumbent_copy_guard.sh"
PRESYNC = ROOT / "scripts" / "verify_before_sync.sh"
DOC = ROOT / "docs" / "MIRROR_SYNC_PATCH.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _git(repo: Path, *args: str) -> str:
    """Run git in a SYNTHETIC repository, and refuse anything else.

    A `.git` that is a FILE points at another repository's git dir, which is what a linked worktree
    is. One copied into a synthetic source on 2026-10-04 aimed `git add -A` and `git commit` at the
    real checkout's branch. So every command but `init` requires this repository's own `.git`
    DIRECTORY, and the ceiling stops git from discovering a repository above it.
    """
    if args[0] != "init":
        assert (repo / ".git").is_dir() and not (
            repo / ".git"
        ).is_symlink(), f"refusing to run git in {repo}: its .git is not its own directory"
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(repo.parent)},
    )
    return proc.stdout


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ORCH_", "VERIFY_BEFORE"))}
    env["GIT_CEILING_DIRECTORIES"] = "/"
    env.update(extra)
    return env


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    """A committed checkout with one of everything the mirror is made of."""
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    _write(src / "src" / "base.py", "BASE = 1\n")
    _write(src / "src" / "other.py", "OTHER = 1\n")
    _write(src / "orchestrate.sh", "echo tick\n", 0o755)
    _write(src / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    _write(src / "tests" / "fixtures" / "f.txt", "fixture\n")
    _write(src / "scripts" / "check_checks_reported.py", "print('ok')\n", 0o644)
    for real in (BUILDER, INSTALLER, GUARD):
        shutil.copy2(real, src / "scripts" / real.name)
    _write(src / "docs" / "guide.md", "guide\n")
    _write(src / ".github" / "workflows" / "ci.yml", "name: ci\n")
    _write(src / ".gitignore", "experiments/\n")
    _write(src / "ruff.toml", "line-length = 100\n")
    for name in ("pyproject.toml", "CLAUDE.md", "AGENTS.md", "ORCHESTRATOR.md"):
        _write(src / name, f"{name}\n")
    _write(src / "IMPROVEMENT_BACKLOG.md", "pointer\n")
    _write(src / ".verify-floor.json", '{"collected": 1}\n')
    _write(src / "config" / "coverage-baseline.json", "{}\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    return src


def _build(src: Path, mirror: Path | None, builder: Path = BUILDER, **env: str):
    argv = ["bash", str(builder), str(src)] + ([str(mirror)] if mirror else [])
    return subprocess.run(argv, env=_env(**env), capture_output=True, text=True, timeout=120)


def _tree(root: Path, skip: tuple[str, ...] = ()) -> dict[str, tuple[int, bytes | None]]:
    """Every entry under `root`: relative path -> (permission bits, bytes or None for a dir)."""
    out: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel in skip:
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        out[rel] = (mode, None if path.is_dir() else path.read_bytes())
    return out


# --------------------------------------------------------------------------- the builder itself


def test_the_mirror_is_flat_and_carries_the_committed_trees(source: Path, tmp_path: Path) -> None:
    # Working-tree edits: a module (ships) and a test (does not: tests/ ships from HEAD).
    (source / "src" / "base.py").write_text("BASE = 2  # uncommitted\n")
    (source / "tests" / "test_a.py").write_text("raise SystemExit('uncommitted')\n")
    (source / "tests" / "test_untracked.py").write_text("UNTRACKED = 1\n")
    mirror = tmp_path / "mirror"
    proc = _build(source, mirror)
    assert proc.returncode == 0, proc.stderr
    assert (mirror / "base.py").read_text() == "BASE = 2  # uncommitted\n"
    assert not (mirror / "src").exists(), "the modules must sit FLAT at the root"
    assert "def test_a" in (mirror / "tests" / "test_a.py").read_text()
    assert not (mirror / "tests" / "test_untracked.py").exists()
    assert (mirror / "tests" / "fixtures" / "f.txt").read_text() == "fixture\n"
    assert "uncommitted test edits are NOT shipped" in proc.stdout
    for executable in ("orchestrate.sh", "scripts/check_checks_reported.py"):
        assert os.access(mirror / executable, os.X_OK), executable
    assert (mirror / ".docs-shipped.txt").read_text() == "docs/guide.md\n"
    for shipped in (
        ".github/workflows/ci.yml",
        ".gitignore",
        "ruff.toml",
        "pyproject.toml",
        "AGENTS.md",
        "ORCHESTRATOR.md",
        "config/coverage-baseline.json",
        ".verify-floor.json",
    ):
        assert (mirror / shipped).is_file(), shipped


def test_docs_merge_keeps_runtime_output_and_drops_a_deleted_doc(
    source: Path, tmp_path: Path
) -> None:
    mirror = tmp_path / "mirror"
    assert _build(source, mirror).returncode == 0
    _write(mirror / "docs" / "reports" / "runtime.md", "written at run time\n")
    victim = tmp_path / "outside.txt"
    victim.write_text("never the mirror's\n")
    with (mirror / ".docs-shipped.txt").open("a") as manifest:
        manifest.write("../outside.txt\ndocs/../../outside.txt\n")
    _git(source, "rm", "-q", "docs/guide.md")
    _write(source / "docs" / "new.md", "new\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "docs")
    proc = _build(source, mirror)
    assert proc.returncode == 0, proc.stderr
    assert (mirror / "docs" / "reports" / "runtime.md").read_text() == "written at run time\n"
    assert not (mirror / "docs" / "guide.md").exists(), "a doc deleted upstream must not linger"
    assert (mirror / "docs" / "new.md").is_file()
    assert victim.read_text() == "never the mirror's\n", "a manifest entry escaped docs/"


def test_refusals_name_their_cause_and_write_nothing(source: Path, tmp_path: Path) -> None:
    # No destination: there is deliberately no default that could name the live mirror.
    nowhere = _build(source, None)
    assert nowhere.returncode == 2 and "usage" in nowhere.stderr, nowhere.stderr
    # A source that is not a git checkout: tests/, scripts/, .github/ and docs/ ship from git.
    plain = tmp_path / "plain"
    shutil.copytree(source, plain, ignore=shutil.ignore_patterns(".git"))
    mirror = tmp_path / "mirror"
    refused = _build(plain, mirror, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert refused.returncode == 1 and "not a git checkout" in refused.stderr, refused.stderr
    assert not mirror.exists(), "a refusal must not have started building"


# --------------------------------------------------------------------------- the contract's consumers


def test_everything_the_builder_ships_is_owned_by_the_installer(tmp_path: Path) -> None:
    """An unowned file is never installed, so the live mirror keeps a stale copy of it forever.

    That was true of AGENTS.md and ORCHESTRATOR.md until 2026-10-04: the copier shipped them for the
    terminal merge-contract test, and `install_verified_snapshot.OWNED_FILES` did not name them. The
    source here is a SUPERSET of what the builder could name, so the builder decides what ships:
    every top-level file of this tree, plus every registry path the builder's own text mentions.
    """
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    for entry in ROOT.iterdir():
        # Never `.git`: in a linked worktree it is a file naming the real repository's git dir.
        if entry.is_file() and entry.suffix != ".py" and entry.name != ".git":
            shutil.copy2(entry, src / entry.name)
    registries = re.findall(r"\b(?:experiments|data|config)/[\w.-]+\.json\b", BUILDER.read_text())
    assert registries, "the builder names no registry, so the superset below covers none"
    for rel in sorted(set(registries)):
        _write(src / rel, "{}\n")
    _write(src / "src" / "base.py", "BASE = 1\n")
    _write(src / "orchestrate.sh", "echo tick\n", 0o755)
    _write(src / "tests" / "test_a.py", "def test_a():\n    pass\n")
    _write(src / "scripts" / "check_checks_reported.py", "print('ok')\n")
    shutil.copy2(INSTALLER, src / "scripts" / INSTALLER.name)
    _write(src / "docs" / "guide.md", "guide\n")
    _write(src / ".github" / "workflows" / "ci.yml", "name: ci\n")
    _git(src, "add", "-A", "--force")
    _git(src, "commit", "-q", "-m", "superset")
    mirror = tmp_path / "mirror"
    proc = _build(src, mirror)
    assert proc.returncode == 0, proc.stderr
    owned = {p.as_posix() for p in installer.owned_entries(mirror)}
    shipped = {
        p.relative_to(mirror).as_posix() for p in mirror.rglob("*") if p.is_file() or p.is_symlink()
    }
    assert shipped, "the builder shipped nothing, so this test would pass vacuously"
    unowned = sorted(shipped - owned)
    assert not unowned, (
        f"the builder ships {unowned} but install_verified_snapshot never installs them, so the "
        "live mirror would keep a stale copy forever: add them to OWNED_FILES"
    )


def test_every_working_tree_input_moves_the_source_identity(source: Path, tmp_path: Path) -> None:
    """A verdict is VOID when the source moves during verification, judged by the identity.

    The identity is a list in verify_before_sync.sh, and the builder is a script, so the two can
    drift: AGENTS.md and ORCHESTRATOR.md were copied from the working tree and missing from the
    identity until 2026-10-04. Which files are WORKING-TREE inputs is decided by behaviour: edit
    every candidate without committing, rebuild, and see which shipped bytes moved.
    """

    def identity() -> str:
        proc = subprocess.run(
            ["bash", str(PRESYNC), "--identity", str(source)],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    clean = tmp_path / "clean"
    assert _build(source, clean).returncode == 0
    candidates = [
        p for p in source.rglob("*") if p.is_file() and ".git" not in p.relative_to(source).parts
    ]
    originals = {p: p.read_bytes() for p in candidates}
    for path in candidates:
        path.write_bytes(originals[path] + b"\n# moved\n")
    edited = tmp_path / "edited"
    try:
        assert _build(source, edited).returncode == 0
    finally:
        for path, data in originals.items():
            path.write_bytes(data)
    before, after = _tree(clean), _tree(edited)
    moved = sorted(rel for rel in after if rel in before and after[rel][1] != before[rel][1])
    assert "base.py" in moved and "AGENTS.md" in moved, moved
    assert not any(rel.startswith(("tests/", "docs/", ".github/")) for rel in moved), moved
    baseline = identity()
    for rel in moved:
        if rel == ".docs-shipped.txt":
            continue
        origin = source / ("src/" + rel if rel.endswith(".py") and "/" not in rel else rel)
        assert origin.is_file(), (rel, origin)
        origin.write_bytes(originals[origin] + b"\n# moved\n")
        try:
            assert identity() != baseline, (
                f"{rel} ships from the working tree, but editing it does not change the source "
                "identity, so a run that saw it change mid-verify would not be VOID"
            )
        finally:
            origin.write_bytes(originals[origin])


# --------------------------------------------------------------------------- the documented copier


def _documented_copier() -> str:
    text = DOC.read_text(encoding="utf-8")
    heading = text.index("**The copier, whole.**")
    start = text.index("```bash\n", heading) + len("```bash\n")
    end = text.index("\n```", start)
    return text[start:end] + "\n"


def _documented_guard() -> str:
    """Extracted exactly as scripts/capture_mirror_deployment_evidence.js extracts it."""
    text = DOC.read_text(encoding="utf-8")
    heading = text.index("### Direct incumbent copier entry guard")
    start = text.index("```bash\n", heading) + len("```bash\n")
    return text[start : text.index("\n```", start)]


def _gh_stub(bin_dir: Path, registry: str) -> None:
    encoded = base64.b64encode(registry.encode()).decode()
    _write(bin_dir / "gh", f"#!/bin/sh\nprintf '%s' '{encoded}'\n", 0o755)


def test_the_documented_copier_builds_through_the_builder(source: Path, tmp_path: Path) -> None:
    copier = tmp_path / "orch-sync-mirror.sh"
    copier.write_text(_documented_copier())
    root = tmp_path / "stage"
    (root / "home" / ".codex" / "orchestrator").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    _gh_stub(bin_dir, '{"repos": []}')
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    staged = dict(PATH=path, HOME=str(root / "home"), ORCH_MIRROR=str(root / "mirror"))
    proc = subprocess.run(
        ["bash", str(copier), str(source)],
        env=_env(ORCH_PRIVATE_COPY_ROOT=str(root), **staged),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "repo_review_registry.json from GitHub main" in proc.stdout
    contract = tmp_path / "contract"
    assert _build(source, contract).returncode == 0
    # The copier adds the registry and nothing else, so CI's tree is the shipped tree.
    assert _tree(root / "mirror", skip=("repo_review_registry.json",)) == _tree(contract)
    assert (root / "mirror" / "repo_review_registry.json").read_text() == '{"repos": []}'
    # A source without the builder is refused BEFORE anything is written.
    (source / "scripts" / "build_exec_mirror.sh").unlink()
    shutil.rmtree(root / "mirror")
    refused = subprocess.run(
        ["bash", str(copier), str(source)],
        env=_env(ORCH_PRIVATE_COPY_ROOT=str(root), **staged),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.returncode == 2 and "NOT SYNCED" in refused.stderr, refused.stderr
    assert not (root / "mirror").exists()


def _with_390_guard(copier: str) -> str:
    """The copier with #390's guard inserted where that section says: right after `MIRROR=`."""
    lines = copier.splitlines(keepends=True)
    (at,) = [i for i, line in enumerate(lines) if line.startswith('MIRROR="${ORCH_MIRROR:-')]
    return "".join(lines[: at + 1]) + _documented_guard() + "\n" + "".join(lines[at + 1 :])


def test_the_390_guard_composes_with_the_documented_copier(source: Path, tmp_path: Path) -> None:
    """The documented copier leaves #390's guard out (it is #389's deployment step), so inserting
    it where that section says must still work, and must leave the exact bytes the
    deployment-evidence collector looks for."""
    assert _documented_guard() not in _documented_copier(), "the guard is #389's step, not this one"
    composed = _with_390_guard(_documented_copier())
    assert _documented_guard() in composed
    copier = tmp_path / "orch-sync-mirror.sh"
    copier.write_text(composed)
    _write(
        source / "scripts" / "publish_unverified_snapshot.sh",
        '#!/usr/bin/env bash\nprintf "publisher %s %s\\n" "$1" "$2" > "$FAKE_RECORD"\n',
    )
    record, live = tmp_path / "record.txt", tmp_path / "live-mirror"
    direct = subprocess.run(
        ["bash", str(copier), str(source)],
        env=_env(HOME=str(tmp_path), ORCH_MIRROR=str(live), FAKE_RECORD=str(record)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert direct.returncode == 0, direct.stderr
    assert record.read_text() == f"publisher {source} {live}\n"
    assert not live.exists(), "the guard must route a direct call away from an in-place copy"
    # A staging call (the verifier's and the publisher's own) still builds through the builder.
    root = tmp_path / "stage"
    (root / "home").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    _gh_stub(bin_dir, '{"repos": []}')
    staged = subprocess.run(
        ["bash", str(copier), str(source)],
        env=_env(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            HOME=str(root / "home"),
            ORCH_MIRROR=str(root / "mirror"),
            ORCH_PRIVATE_COPY_ROOT=str(root),
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert staged.returncode == 0, staged.stderr
    assert (root / "mirror" / "base.py").is_file()


def test_the_documented_install_command_writes_the_documented_copier(tmp_path: Path) -> None:
    """The one-liner the doc tells the owner to run must write exactly the block above it."""
    text = DOC.read_text(encoding="utf-8")
    heading = text.index("**The copier, whole.**")
    (command,) = [
        block.split("\n```", 1)[0]
        for block in text[heading:].split("```bash\n")[1:]
        if block.startswith("python3 -c")
    ]
    home = tmp_path / "home"
    (home / ".codex" / "bin").mkdir(parents=True)
    (home / ".codex" / "orchestrator-src" / "docs").mkdir(parents=True)
    shutil.copy2(DOC, home / ".codex" / "orchestrator-src" / "docs" / DOC.name)
    proc = subprocess.run(
        ["bash", "-c", command],
        env=_env(HOME=str(home)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert (home / ".codex" / "bin" / "orch-sync-mirror.sh").read_text() == _documented_copier()


# --------------------------------------------------------------------------- the pre-sync FYI line

FAKE_VERIFY = """import os, sys
print("  tree:       " + os.environ.get("FAKE_TREE", "EXEC MIRROR — mirror_* ceilings apply"))
print("  VERIFIED — fake")
"""


@pytest.mark.parametrize("copier", ["builder", "documented", "drifted"])
def test_presync_says_whether_ci_verified_the_shipped_tree(
    source: Path, tmp_path: Path, copier: str
) -> None:
    """FYI only: the verdict stays the copier's own, whatever this line says."""
    _write(source / "src" / "verify.py", FAKE_VERIFY)
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "fake verify")
    bin_dir = tmp_path / "bin"
    _gh_stub(bin_dir, '{"repos": []}')
    if copier == "builder":
        script = source / "scripts" / "build_exec_mirror.sh"
    else:
        script = tmp_path / "copier.sh"
        body = _documented_copier()
        if copier == "drifted":
            body += 'printf "only here\\n" > "$MIRROR/extra.txt"\n'
        script.write_text(body)
    home, runtime = tmp_path / "home", tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    proc = subprocess.run(
        ["bash", str(PRESYNC), str(source)],
        env=_env(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            HOME=str(home),
            TMPDIR=str(tmp_path),
            ORCH_SYNC_SCRIPT=str(script),
            ORCH_LOCAL_RUNTIME=str(runtime),
            ORCH_STATE_DIR=str(runtime),
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "verify-before-sync: VERIFIED" in proc.stdout
    expected = {
        "builder": "built by scripts/build_exec_mirror.sh itself",
        "documented": "built exactly the tree scripts/build_exec_mirror.sh builds",
        "drifted": "built DIFFERENT trees",
    }[copier]
    assert expected in proc.stdout, proc.stdout[-3000:]
    if copier == "drifted":
        assert "extra.txt" in proc.stdout, "the line must name what differs"


# --------------------------------------------------------------------------- the CI job's wiring


def test_ci_verifies_the_contract_in_the_bare_shape_with_one_procedure() -> None:
    """The job and the labels are a pair of literals across a YAML file and a module, so pin them.

    The job must run THE pre-sync script on THE builder, expect the bare label verify.py prints, and
    point both state variables at scratch. The script's default must stay the owner's provisioned
    label, so a sync can never be verified under the bare shape's larger ceilings.
    """
    text = CI.read_text(encoding="utf-8")
    # The job's block, read as text: from its key at two-space indent to the next job's key.
    # (No YAML parser: the Gate refuses test imports that pyproject does not declare.)
    start = text.index("\n  exec-mirror:\n")
    nxt = re.search(r"\n  [A-Za-z0-9_-]+:\n", text[start + 1 :])
    block = text[start : start + 1 + nxt.start()] if nxt else text[start:]
    env = dict(re.findall(r"^\s{10}([A-Z_]+):\s*(.+?)\s*$", block, flags=re.M))
    assert "scripts/verify_before_sync.sh" in block, block
    assert env["ORCH_SYNC_SCRIPT"].endswith("/scripts/build_exec_mirror.sh"), env
    label = env["VERIFY_BEFORE_SYNC_TREE"]
    assert verify.TREE_LABELS[verify.BARE_EXEC_MIRROR].startswith(label + " "), env
    assert env["VERIFY_BEFORE_SYNC_FLOOR_MAY_LAG"].strip("'\"") == "1", env
    assert {"ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"} <= set(env), env
    defaults = re.findall(
        r"VERIFY_BEFORE_SYNC_TREE:-([^}]+)\}", PRESYNC.read_text(encoding="utf-8")
    )
    assert len(defaults) == 1, defaults
    assert verify.TREE_LABELS[verify.EXEC_MIRROR].startswith(defaults[0] + " "), defaults


def test_the_git_helper_refuses_a_borrowed_git_dir(tmp_path: Path) -> None:
    """The guard that stopped this file committing into the real checkout, pinned on its own."""
    borrowed = tmp_path / "borrowed"
    borrowed.mkdir()
    (borrowed / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere'}\n")
    with pytest.raises(AssertionError, match="is not its own directory"):
        _git(borrowed, "add", "-A")
