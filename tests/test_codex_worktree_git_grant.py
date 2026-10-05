"""A codex run that commits from its linked worktree is granted that worktree's git dir (2026-10-04).

THE DEFECT. `provision.py` makes every dispatch worktree a LINKED worktree of one canonical clone,
so the worktree's `.git` is a file naming its private git dir, `<common>/worktrees/<name>`, which
holds `index`, `HEAD` and the HEAD reflog. Codex 0.158 began carving the dir that file names out
of every writable root (`include_resolved_gitdirs`, codex-rs `protocol/src/permissions.rs`), so
under `--sandbox workspace-write` a commit there fails with `index.lock: Operation not permitted`
even though `~/.codex`, which holds the canonical clone, is a writable root. In-place commits
succeeded in 31 dispatch logs from 2026-08-16 to 2026-09-14 (codex 0.147.0 to 0.153.4). The first
codex 0.160.0 batch (2026-10-04T19:18Z) could not commit: Manager-Database#1750 and
Fine-Art-Archive#770 re-cloned into /tmp and pushed from there, so the push record
`pushed_branches.py` reads from the run's own worktree had nothing to see.

THE RULE. A run whose job is to commit (`commits_in_worktree=True`: a dispatch or an experiment
arm, never an offload) is built with `--add-dir` for exactly the private git dir, spelled as the
`.git` file spells it, because codex drops its carve-out only for an explicit entry with that path.
It also gets `objects`, `refs` and `logs` of the common dir, for a sandbox whose roots do not already
cover the clone. It never gets the common dir itself, which holds `config` and `hooks/`.

HOW THIS FILE PROVES IT WITHOUT CODEX, which no CI runner has. The real-codex runs are in the PR.
Here, file permissions stand in for the seatbelt. Everything in the common git dir is made
read-only except the granted roots. A commit and a push must then succeed, and the push recorder
must see them. Withholding any ONE root must break that, so every root is necessary.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import adapters
import env_prereq
import execution_profiles
import paths
import provision
import pushed_branches

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
TARGET, LANE = "o/r#8", "opener"
BRANCH = "orchestrator/issue-8"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        check=check,
    )


class Layout:
    """A bare origin, the canonical clone and one linked worktree, where provision.py puts them."""

    def __init__(self, runtime: Path):
        self.runtime = runtime
        runtime.mkdir(parents=True)
        self.origin = runtime / "origin.git"
        _git(runtime, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = runtime / "seed"
        seed.mkdir()
        _git(seed, "init", "-q", "-b", "main")
        (seed / "a").write_text("a\n")
        _git(seed, "add", "a")
        _git(seed, "commit", "-qm", "init")
        _git(seed, "push", "-q", str(self.origin), "main")
        self.canon = runtime / "repos" / provision.repo_slug("o/r")
        self.canon.parent.mkdir(parents=True)
        _git(runtime, "clone", "-q", str(self.origin), str(self.canon))
        self.wt = runtime / "worktrees" / provision.worktree_name(TARGET, LANE)
        self.wt.parent.mkdir(parents=True)
        _git(self.canon, "worktree", "add", "-q", "-b", BRANCH, str(self.wt), "origin/main")
        self.common = Path(_git(self.wt, "rev-parse", "--git-common-dir").stdout.strip())
        if not self.common.is_absolute():
            self.common = (self.wt / self.common).resolve()

    @property
    def gitdir_as_written(self) -> str:
        """The private git dir exactly as the `.git` file names it, which is how codex reads it."""
        return (self.wt / ".git").read_text().split(":", 1)[1].strip()

    @property
    def expected_roots(self) -> list[str]:
        """The grant, derived without the code under test: that git dir, then the shared dirs."""
        shared = [str(self.common / name) for name in ("objects", "refs", "logs")]
        return [self.gitdir_as_written, *shared]


@pytest.fixture
def layout(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(adapters.CODEX_GIT_GRANT_DISABLED_ENV, raising=False)
    return Layout(tmp_path / "runtime")


def _add_dirs(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]


def _inside(path: Path, roots: list[str]) -> bool:
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


def test_the_grant_is_the_worktree_git_dir_and_three_shared_dirs(layout):
    roots = adapters.codex_worktree_git_roots(layout.wt)
    common = layout.common
    assert roots == layout.expected_roots, roots
    # git's own answer names the same directory codex will protect.
    git_dir = _git(layout.wt, "rev-parse", "--absolute-git-dir").stdout.strip()
    assert Path(roots[0]).resolve() == Path(git_dir).resolve(), (roots[0], git_dir)
    # What git EXECUTES later, outside any sandbox, stays out of every root.
    for kept_read_only in ("config", "hooks", "info", "packed-refs", "HEAD"):
        assert not _inside(common / kept_read_only, roots), (kept_read_only, roots)
    assert not _inside(common, roots), roots


def test_build_command_grants_only_a_committing_workspace_write_codex_run(
    layout, monkeypatch, tmp_path
):
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", "0")
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    roots = layout.expected_roots

    granted = adapters.build_command("codex", "x", cwd=layout.wt, commits_in_worktree=True)
    assert _add_dirs(granted) == roots, granted
    assert granted[granted.index("--sandbox") + 1] == "workspace-write", granted

    before = adapters.build_command("codex", "x", cwd=layout.wt)
    assert _add_dirs(before) == [], "a run that does not commit was granted git paths"
    assess = adapters.build_command(
        "codex", "x", mode="assess", cwd=layout.wt, commits_in_worktree=True
    )
    assert assess[assess.index("--sandbox") + 1] == "read-only" and _add_dirs(assess) == [], assess

    # A profile narrowed to read-only stays read-only. The profile binary only has to exist.
    fake_bin = tmp_path / "codex-profile-bin"
    fake_bin.write_text("")
    monkeypatch.setattr(adapters, "CODEX_PROFILE_BIN", fake_bin)
    profile = execution_profiles.profiles_for_agent("codex")[0]
    narrowed = adapters.build_command(
        "codex",
        "x",
        cwd=layout.wt,
        profile=profile,
        permission_mode="read-only",
        commits_in_worktree=True,
    )
    assert narrowed[narrowed.index("--sandbox") + 1] == "read-only", narrowed
    assert _add_dirs(narrowed) == [], narrowed
    profiled = adapters.build_command(
        "codex", "x", cwd=layout.wt, profile=profile, commits_in_worktree=True
    )
    assert _add_dirs(profiled) == roots, profiled

    # The outer-seat bypass has no sandbox to widen.
    monkeypatch.setenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", "1")
    bypass = adapters.build_command("codex", "x", cwd=layout.wt, commits_in_worktree=True)
    assert "--dangerously-bypass-approvals-and-sandbox" in bypass and _add_dirs(bypass) == []
    monkeypatch.setenv("ORCH_CODEX_BYPASS_INNER_SANDBOX", "0")

    # The kill switch restores the argv byte for byte.
    monkeypatch.setenv(adapters.CODEX_GIT_GRANT_DISABLED_ENV, "1")
    assert adapters.codex_worktree_git_roots(layout.wt) == []
    disabled = adapters.build_command("codex", "x", cwd=layout.wt, commits_in_worktree=True)
    assert disabled == before, (disabled, before)
    monkeypatch.delenv(adapters.CODEX_GIT_GRANT_DISABLED_ENV)

    # Only codex has this sandbox. gemini keeps exactly its own confinement, the workspace itself.
    claude = adapters.build_command("claude", "x", cwd=layout.wt, commits_in_worktree=True)
    assert _add_dirs(claude) == [], claude
    gemini = adapters.build_command("gemini", "x", cwd=layout.wt, commits_in_worktree=True)
    assert _add_dirs(gemini) == [str(adapters.workspace_path(layout.wt))], gemini


def test_no_grant_outside_a_linked_worktree(layout, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert adapters.codex_worktree_git_roots(plain) == []
    assert adapters.codex_worktree_git_roots(None) == []
    # A regular clone: its `.git` is a DIRECTORY, and the only grant that commits there is all of
    # it, config and hooks included.
    assert adapters.codex_worktree_git_roots(layout.canon) == []

    def pointing_at(name: str, text: str) -> Path:
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / ".git").write_text(text)
        return workspace

    # A submodule-shaped pointer names a whole repository: no `commondir` file.
    assert adapters.codex_worktree_git_roots(pointing_at("sub", f"gitdir: {layout.common}\n")) == []
    # A private dir that does not sit at `<common>/worktrees/<name>`.
    stray = tmp_path / "elsewhere" / "wt9"
    stray.mkdir(parents=True)
    (stray / "commondir").write_text(f"{layout.common}\n")
    assert adapters.codex_worktree_git_roots(pointing_at("stray", f"gitdir: {stray}\n")) == []
    assert adapters.codex_worktree_git_roots(pointing_at("junk", "not a pointer\n")) == []
    assert adapters.codex_worktree_git_roots(pointing_at("empty", "gitdir:   \n")) == []
    missing = tmp_path / "gone" / "worktrees" / "x"
    assert adapters.codex_worktree_git_roots(pointing_at("gone", f"gitdir: {missing}\n")) == []


def test_a_relative_gitdir_pointer_resolves_against_the_worktree(layout):
    absolute = Path(layout.gitdir_as_written)
    (layout.wt / ".git").write_text(f"gitdir: {os.path.relpath(absolute, layout.wt)}\n")
    assert _git(layout.wt, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    roots = adapters.codex_worktree_git_roots(layout.wt)
    assert roots and roots[0] == os.path.normpath(absolute), roots


def _permissions_unenforced(scratch: Path) -> str | None:
    """Reason string when this user ignores file modes, so chmod cannot stand in for the sandbox."""
    probe = scratch / "mode-probe"
    probe.mkdir()
    probe.chmod(0o555)
    try:
        (probe / "x").write_text("")
    except PermissionError:
        return None
    finally:
        probe.chmod(0o755)
    return "file modes are not enforced for this user (root?), so chmod cannot simulate codex"


@contextmanager
def _sandboxed(common: Path, writable: list[str]):
    """Make the common git dir read-only except `writable`, which is what the seatbelt enforces."""
    changed: list[tuple[Path, int]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(common):
            here = Path(dirpath)
            if _inside(here, writable):
                dirnames[:] = []
                continue
            for name in filenames:
                path = here / name
                if not path.is_symlink() and not _inside(path, writable):
                    changed.append((path, path.stat().st_mode))
                    path.chmod(0o444)
            changed.append((here, here.stat().st_mode))
            here.chmod(0o555)
        yield
    finally:
        for path, mode in reversed(changed):
            path.chmod(stat.S_IMODE(mode))


# Where git stops when one root is withheld, measured with git 2.49. The private git dir is the
# production refusal itself (Manager-Database#1750, Fine-Art-Archive#770).
REFUSED_AT = {
    "gitdir": ("add", "/index.lock"),
    "objects": ("add", "/objects"),
    "refs": ("commit", "/refs/heads/"),
    "logs": ("commit", "/logs/refs/heads/"),
}


@pytest.mark.parametrize("withheld", [None, *REFUSED_AT])
def test_a_commit_and_push_need_every_granted_root_and_nothing_else(layout, tmp_path, withheld):
    env_prereq.require(_permissions_unenforced(tmp_path))
    roots = adapters.codex_worktree_git_roots(layout.wt)
    names = dict(zip(["gitdir", "objects", "refs", "logs"], roots))
    writable = [root for name, root in names.items() if name != withheld]
    start = int(time.time()) - 1
    (layout.wt / "change.txt").write_text("change\n")
    steps: dict[str, int] = {}
    errors: dict[str, str] = {}
    with _sandboxed(layout.common, writable):
        for step, args in (
            ("add", ["add", "-A"]),
            ("commit", ["commit", "-qm", "change"]),
            ("push", ["push", "-q", "origin", "HEAD"]),
        ):
            done = _git(layout.wt, *args, check=False)
            steps[step], errors[step] = done.returncode, done.stderr
            if done.returncode:
                break
    head = _git(layout.wt, "rev-parse", "HEAD").stdout.strip()
    record = pushed_branches.read_pushes(layout.wt, start, int(time.time()) + 1)
    seen = {(b["branch"], b["sha"]) for b in record["branches"]}
    if withheld is None:
        assert steps == {"add": 0, "commit": 0, "push": 0}, (steps, errors)
        assert (BRANCH, head) in seen, ("the push recorder did not see the push", record)
        assert _git(layout.origin, "rev-parse", BRANCH).stdout.strip() == head
        # Git may try to pack refs after a commit. That is best-effort: it prints an error and the
        # commit still succeeds, as it did under codex. Any OTHER refused write is a missing root.
        refused = [line for line in errors["commit"].splitlines() if "Unable to create" in line]
        assert all("packed-refs.lock" in line for line in refused), refused
    else:
        step, path = REFUSED_AT[withheld]
        assert steps.get(step) not in (None, 0), (f"{withheld} withheld, {step} still ran", steps)
        assert path in errors[step], (withheld, errors[step][-400:])
        assert (BRANCH, head) not in seen, record


PLAN = """
import json, sys
import dispatcher, execution_profiles

base = {"agent": "codex", "target": sys.argv[1], "task_type": "implement", "mode": "full",
        "lane": sys.argv[2], "prompt": "Implement the change."}
profiled = dict(base, selected_profile_id=execution_profiles.profiles_for_agent("codex")[0][
    "profile_id"])
out = {}
for name, assignment in (("plain", base), ("profiled", profiled)):
    d = dispatcher.plan_dispatch(assignment, dry_run=True)
    out[name] = {"cwd": d["cwd"], "argv": d["argv"]}
print("RESULT " + json.dumps(out))
"""


def test_plan_dispatch_grants_the_worktree_it_provisioned(layout, tmp_path):
    """The dispatch path the 19:18Z batch took, with and without an exact profile.

    A child interpreter, because every runtime path is fixed from the environment at import. It
    inherits no ORCH_* variable and no CODEX_SANDBOX, so nothing points it back at live state or
    switches the sandbox off (test_gemini_workspace_one_spelling.py has the reasoning).
    """
    fake_bin = tmp_path / "codex-profile-bin"
    fake_bin.write_text("")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ORCH_") and k not in ("HANDOFF_DIR", "CODEX_SANDBOX")
    }
    env.update(
        GIT_ENV,
        HOME=str(tmp_path / "home"),
        HANDOFF_DIR=str(tmp_path / "handoff"),
        ORCH_LOCAL_RUNTIME=str(layout.runtime),
        ORCH_STATE_DIR=str(layout.runtime / "state"),
        ORCH_MODEL_PROBE="0",
        ORCH_CODEX_BYPASS_INNER_SANDBOX="0",
        ORCH_CODEX_PROFILE_BIN=str(fake_bin),
        PYTHONPATH=str(paths.MODULE_DIR),
    )
    env.update(
        {
            var: str(tmp_path / "registries" / name)
            for var, name in paths.SEEDED_REGISTRY_ENV.items()
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", PLAN, TARGET, LANE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and len(lines) == 1, (proc.stdout[-3000:], proc.stderr[-3000:])
    out = json.loads(lines[0][len("RESULT ") :])
    roots = layout.expected_roots
    for name, planned in out.items():
        assert Path(planned["cwd"]) == layout.wt.resolve(), (name, planned["cwd"])
        argv = planned["argv"]
        assert argv[argv.index("--sandbox") + 1] == "workspace-write", (name, argv[:12])
        assert _add_dirs(argv) == roots, (name, _add_dirs(argv), roots)
