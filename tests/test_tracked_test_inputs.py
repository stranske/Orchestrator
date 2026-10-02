"""tests/conftest.py's guard: a test may not read a file under tests/ that the exec mirror lacks.

`orch-sync-mirror.sh` ships tests/ as `git archive HEAD tests` (2026-10-02), so a test's input
reaches the mirror exactly when git puts it in that archive. The guard asks git for the complement
once per session and fails any test that reads a file in it; see its docstring for the incident.
These tests pin three things: what the guard watches is exactly what the archive leaves out, a read
of one of those files fails the reading test in a real pytest session, and the live session armed
wherever git can answer.

Every check that needs git builds its own scratch repository, so none of them depends on the tree
it runs in and none skips in the exec mirror, which is a file copy rather than a checkout. The
mirror's skip ceiling is unchanged by this file.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import env_prereq

CONFTEST = Path(__file__).resolve().parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("tracked_inputs_guard_under_test", CONFTEST)
assert _spec is not None and _spec.loader is not None
guard_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = guard_mod  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(guard_mod)

# Scratch repositories must not see the owner's git configuration: a global excludes file would
# turn an "untracked" fixture into an "ignored" one, and a commit hook could fail the setup.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "guard",
    "GIT_AUTHOR_EMAIL": "guard@example.invalid",
    "GIT_COMMITTER_NAME": "guard",
    "GIT_COMMITTER_EMAIL": "guard@example.invalid",
}

READS = r"""
import shutil
from pathlib import Path

HERE = Path(__file__).parent


def test_tracked():
    assert (HERE / "fixtures" / "tracked.txt").read_text() == "tracked\n"


def test_force_added():
    (HERE / "data" / "forced.json").read_text()


def test_forgotten():
    (HERE / "fixtures" / "forgotten.txt").read_text()


def test_swallowed():
    (HERE / "data" / "swallowed.json").read_bytes()


def test_held_back():
    with open(HERE / "fixtures" / "held_back.txt") as fh:
        fh.read()


def test_copytree_over_junk(tmp_path):
    shutil.copytree(HERE / "junky", tmp_path / "copy")


def test_own_output():
    out = HERE / "fixtures" / "leftover.txt"
    out.write_text("fresh\n")
    assert out.read_text() == "fresh\n"


def test_fails_anyway():
    (HERE / "fixtures" / "forgotten.txt").read_text()
    assert False, "its own failure"
"""

AT_IMPORT = r"""
from pathlib import Path

DATA = (Path(__file__).parent / "fixtures" / "forgotten.txt").read_text()


def test_never_collected():
    pass
"""


def require_git() -> None:
    env_prereq.require(
        None
        if shutil.which("git")
        else "git is not on PATH; these checks build a scratch repository to ask git what "
        "`git archive` ships"
    )


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def build_checkout(root: Path, env: dict[str, str]) -> Path:
    """A checkout whose tests/ holds one file of every kind the guard has to tell apart."""

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], env=env, check=True, capture_output=True)

    tests = root / "tests"
    put(root / ".gitignore", "data/\n")
    put(root / ".gitattributes", "tests/fixtures/held_back.txt export-ignore\n")
    put(tests / "conftest.py", CONFTEST.read_text())
    put(tests / "test_reads.py", READS)
    put(tests / "test_reads_at_import.py", AT_IMPORT)
    put(tests / "fixtures" / "tracked.txt", "tracked\n")
    put(tests / "fixtures" / "held_back.txt", "held\n")
    put(tests / "junky" / "a.txt", "kept\n")
    put(tests / "data" / "forced.json", "{}\n")
    git("init", "-q", ".")
    git("add", "-A")
    git("add", "-f", "tests/data/forced.json")  # under an ignored directory, yet tracked
    git("commit", "-q", "-m", "init")
    # On disk, never committed:
    put(tests / "fixtures" / "forgotten.txt", "forgotten\n")
    put(tests / "fixtures" / "leftover.txt", "stale\n")
    put(tests / "data" / "swallowed.json", "{}\n")
    put(tests / "junky" / "a (Tim Stranske's conflicted copy 2026-10-02).txt", "junk\n")
    put(tests / "junky" / ".DS_Store", "finder\n")
    put(tests / "__pycache__" / "stale.cpython-312.pyc", "")
    put(tests / "helper_not_yet_added.py", "")
    return tests


def scratch_env(tmp_path: Path) -> dict[str, str]:
    # The ceiling keeps git from finding a repository ABOVE the scratch tree.
    return {**GIT_ENV, "GIT_CEILING_DIRECTORIES": os.path.realpath(tmp_path)}


def test_effect_reads_the_events_this_interpreter_raises(tmp_path):
    """Classify the audit events Python ACTUALLY raises, in this interpreter, for each access.

    The guard depends on the mode and flags Python attaches to its "open" event, and CI runs a
    different Python from the owner's machine, so this asks the running interpreter rather than
    trusting a table written from one version.
    """
    probe = r"""
import importlib.util, io, json, os, pathlib, shutil, sys
spec = importlib.util.spec_from_file_location("g", sys.argv[1])
g = importlib.util.module_from_spec(spec)
sys.modules["g"] = g
spec.loader.exec_module(g)
src = pathlib.Path(sys.argv[2]) / "input.txt"
src.write_text("x")
seen = []
def hook(event, args):
    if event != "open" or isinstance(args[0], int):
        return
    if os.path.abspath(os.fsdecode(args[0])) == str(src):
        seen.append(g.effect(args[1], args[2]))
sys.addaudithook(hook)
out = {}
def probe(label, fn):
    seen.clear()
    fn()
    out[label] = sorted(set(seen))
probe("open r", lambda: open(src).read())
probe("open rb", lambda: open(src, "rb").read())
probe("Path.read_text", src.read_text)
probe("Path.read_bytes", src.read_bytes)
probe("os.open O_RDONLY", lambda: os.close(os.open(src, os.O_RDONLY)))
probe("io.open_code", lambda: io.open_code(str(src)).read())
probe("shutil.copy source", lambda: shutil.copy(src, src.with_name("copy.txt")))
probe("open r+", lambda: open(src, "r+").read())
probe("open a+", lambda: open(src, "a+").read())
probe("open a", lambda: open(src, "a").write("y"))
probe("open w", lambda: open(src, "w").write("x"))
probe("Path.write_text", lambda: src.write_text("x"))
probe("os.open O_WRONLY|O_TRUNC", lambda: os.close(os.open(src, os.O_WRONLY | os.O_TRUNC)))
print(json.dumps(out))
"""
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(CONFTEST), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    reads = [
        "open r",
        "open rb",
        "Path.read_text",
        "Path.read_bytes",
        "os.open O_RDONLY",
        "io.open_code",
        "shutil.copy source",
        "open r+",
        "open a+",
    ]
    truncates = ["open w", "Path.write_text", "os.open O_WRONLY|O_TRUNC"]
    expected = {
        **{label: ["read"] for label in reads},
        **{label: ["truncate"] for label in truncates},
        "open a": ["other"],
    }
    assert seen == expected, (
        f"Python {sys.version.split()[0]} raises different 'open' events than the guard expects, "
        f"so it would miss reads or watch writes. Got {seen}"
    )


def test_arm_watches_exactly_what_git_archive_leaves_out(tmp_path, monkeypatch):
    require_git()
    env = scratch_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "checkout"
    tests = build_checkout(root, {**os.environ, **env})

    guard = guard_mod.arm(tests)

    assert guard.armed, guard.detail
    watched = dict(guard.watched.values())
    assert watched == {
        "fixtures/forgotten.txt": "untracked",
        "fixtures/leftover.txt": "untracked",
        "data/swallowed.json": "ignored",
        "fixtures/held_back.txt": "export-ignore",
    }, (
        "the guard must watch the untracked, the .gitignore'd and the export-ignore'd inputs, and "
        "nothing it can never matter for: a tracked file under an ignored directory ships, and "
        "Dropbox/Finder junk, .py and .pyc are out of scope by design"
    )
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", "HEAD", "tests"],
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        shipped = {m.name.removeprefix("tests/") for m in tar.getmembers() if m.isfile()}
    on_disk = {p.relative_to(tests).as_posix() for p in tests.rglob("*") if p.is_file()}
    assert set(watched) == {rel for rel in on_disk - shipped if guard_mod.in_scope(rel)}, (
        "the guard and orch-sync-mirror.sh must answer ONE question: what `git archive HEAD tests` "
        f"leaves out. Archive shipped {sorted(shipped)}; the guard watched {sorted(watched)}"
    )


def test_arm_stands_down_where_git_cannot_answer_for_the_tree(tmp_path, monkeypatch):
    require_git()
    for key, value in scratch_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    copied = tmp_path / "file-copy" / "tests"
    copied.mkdir(parents=True)
    guard = guard_mod.arm(copied)
    assert not guard.armed and not guard.watched
    assert "is not a git checkout" in guard.detail, guard.detail

    # A tests/ inside SOMEONE ELSE'S repository: that index says nothing about this tree.
    outer = tmp_path / "outer"
    vendored = outer / "vendored" / "tests"
    vendored.mkdir(parents=True)
    subprocess.run(["git", "-C", str(outer), "init", "-q", "."], check=True, capture_output=True)
    guard = guard_mod.arm(vendored)
    assert not guard.armed and not guard.watched
    assert "sits inside a repository rooted at" in guard.detail, guard.detail


def test_this_session_armed_exactly_where_git_can_answer(tracked_inputs_guard):
    """The live guard: armed in every checkout (CI's included), standing down in the exec mirror.

    A guard that silently stopped arming would pass everything; this makes it a red instead.
    """
    guard = tracked_inputs_guard
    assert guard is not None, "tests/conftest.py never armed: its pytest_configure did not run"
    absent = env_prereq.git_repo_absent()
    if absent is None:
        assert (
            guard.armed
        ), f"git can answer for this tree, yet the guard stood down: {guard.detail}"
    else:
        assert (
            not guard.armed and not guard.watched
        ), f"git cannot answer here ({absent}), yet the guard claims to be watching: {guard.detail}"


def _quiet(text: str) -> str:
    # This file's own output must not look like a pytest summary: verify.py parses every
    # "<n> failed" / "FAILED ..." it can find in the run's output.
    text = re.sub(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)", r"\1_\2", text)
    return "\n".join(f"  | {line}" for line in text.splitlines())


def test_reading_an_input_the_mirror_lacks_fails_that_test(tmp_path):
    """End to end, in a real pytest session over a scratch checkout using the real conftest.py."""
    require_git()
    env = {**os.environ, **scratch_env(tmp_path)}
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    root = tmp_path / "checkout"
    build_checkout(root, env)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider"]
        + ["--continue-on-collection-errors", "tests"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    out = proc.stdout + proc.stderr
    outcomes = {}
    for line in out.splitlines():
        word, _, rest = line.partition(" ")
        if word in ("PASSED", "FAILED", "ERROR"):
            outcomes[rest.split(" - ")[0]] = word
    reads = "tests/test_reads.py::"
    assert outcomes == {
        reads + "test_tracked": "PASSED",
        reads + "test_force_added": "PASSED",
        reads + "test_copytree_over_junk": "PASSED",
        reads + "test_own_output": "PASSED",
        reads + "test_forgotten": "FAILED",
        reads + "test_swallowed": "FAILED",
        reads + "test_held_back": "FAILED",
        reads + "test_fails_anyway": "FAILED",
        "tests/test_reads_at_import.py": "ERROR",
    }, _quiet(out)
    for why in (
        "tests/fixtures/forgotten.txt: not tracked by git",
        "tests/data/swallowed.json: matched by .gitignore",
        "tests/fixtures/held_back.txt: tracked, but .gitattributes marks it export-ignore",
        "AssertionError: its own failure",  # a test that fails anyway keeps its own story
        "tracked test inputs: armed -- watching 4 file(s)",
    ):
        assert why in out, f"missing {why!r}:\n{_quiet(out)}"
