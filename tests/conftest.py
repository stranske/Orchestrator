"""Suite-wide guard: no test may read a file under tests/ that the exec mirror will not have.

THE INCIDENT (2026-10-02). PR #349 added `tests/fixtures/ux_review_adversarial_2026_09_22.txt` and a
test that read it. The file was tracked, so the test passed in every checkout and in CI, and it
was the one failure of the exec mirror's `verify.py` (`FileNotFoundError`): `orch-sync-mirror.sh`
shipped `tests/*.py` and `tests/rail_exercises` and nothing else under `tests/`. #365 inlined that
capture.
The sync now ships the whole tree as `git archive HEAD tests`, so every file git puts in that
archive travels without anyone naming it.

That leaves one way for a test's input to be missing from the mirror: git leaves it out of the
archive. There are three cases, and CI sees none of them when the test skips on a bare runner (no
agent CLIs, no ledger; see `env_prereq`), while the mirror has every local prerequisite and runs it:

* **untracked**: written beside the test and never `git add`ed;
* **ignored**: swallowed by a `.gitignore` pattern (`data/`, `*.db`, `*.lock` and others match
  under `tests/` too), so `git add tests/` skips it without a word;
* **export-ignore**: tracked, but a `.gitattributes` rule keeps it out of every `git archive`. That
  file is synced into this repo from Workflows, so the rule can arrive without anyone here
  deciding it.

Once per session this guard asks git for that set and fails any test that READS a file in it. A
test that would have passed here and failed in the mirror after merge now fails here first, and the
failure names the file and the fix.

Design notes:

* **It watches reads as they happen; it does not grep test sources.** A test's inputs are known for
  certain only when they are opened, and the `sys.addaudithook` "open" event fires once for every
  read path the suite uses: `open`, `Path.read_text`/`read_bytes`, `os.open`, `io.open_code`,
  `shutil.copy`/`copytree` (measured on 3.12).
* **Only reads count, and only of files present when the session started.** A truncating write
  takes the file out of the set, because what the test reads back afterwards is its own output.
* **`.py` is out of scope.** Imports raise the same event for every module and its `.pyc`, and the
  collection floor already catches a test module CI cannot see, since `collected` is an EQUALITY.
* **OS and Dropbox junk is out of scope.** `.DS_Store` and "... conflicted copy ..." files are never
  a test's input, and the owner's Dropbox checkout holds about twenty under `tests/`. One sits in
  the contract directory that `tests/test_model_profile_trial.py` copies, so watching them would
  turn that checkout red over files `git archive` already leaves out.
* **The verdict is a failed REPORT, not an exception from `open()`.** The code under test could
  swallow an exception, but it cannot un-fail a report, and `verify.py` reads the counts.
* **With nothing to watch, it installs no hook.** In CI and in a clean worktree the set is empty,
  so the suite pays nothing there.
* **It is disarmed where git cannot answer, and that loses nothing.** The exec mirror is a file
  copy whose `tests/` IS the archive, so every file in it was tracked at the synced commit.
  `test_tracked_test_inputs.py` asserts the guard is armed exactly when git can answer, which makes
  a guard that silently stopped arming in a checkout a red rather than a quiet pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

WHY = {
    "untracked": "not tracked by git: `git add` it",
    "ignored": (
        "matched by .gitignore, so `git add tests/` skips it: narrow the pattern or `git add -f` it"
    ),
    "export-ignore": (
        "tracked, but .gitattributes marks it export-ignore, so `git archive` leaves it out"
    ),
}


@dataclass
class Guard:
    armed: bool
    detail: str
    # Every absolute spelling of a watched path -> (its path under tests/, why it would not ship).
    watched: dict[str, tuple[str, str]] = field(default_factory=dict)


def is_junk(name: str) -> bool:
    """Files the OS and Dropbox create beside real ones. Never a test's input, never shipped."""
    return name == ".DS_Store" or " conflicted copy" in name


def in_scope(rel: str) -> bool:
    name = os.path.basename(rel)
    return not name.endswith((".py", ".pyc")) and not is_junk(name)


def effect(mode: str | None, flags: int) -> str:
    """'read', 'truncate' or 'other', from an audit "open" event's mode and flags.

    Python reports the mode without its 'b' ('r', 'r+', 'w', 'a', ...), and None for `os.open`,
    whose intent is carried by the flags alone.
    """
    if mode is None:
        if flags & os.O_TRUNC:
            return "truncate"
        return "other" if (flags & os.O_ACCMODE) == os.O_WRONLY else "read"
    if "w" in mode:
        return "truncate"
    if "r" in mode or "+" in mode:
        return "read"
    return "other"


def _split(out: bytes) -> list[str]:
    return [os.fsdecode(p) for p in out.split(b"\0") if p]


def arm(tests_dir: Path) -> Guard:
    """Ask git which files under `tests_dir` `git archive HEAD` would leave out of the mirror."""
    if shutil.which("git") is None:
        return Guard(False, "git is not on PATH, so nothing can say which files are tracked")

    def git(*args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(tests_dir), *args],
            input=stdin,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def failed(proc: subprocess.CompletedProcess[bytes]) -> Guard:
        cmd = " ".join(str(a) for a in proc.args[3:5])
        return Guard(False, f"`{cmd}` failed in {tests_dir}: {os.fsdecode(proc.stderr).strip()}")

    found: dict[str, str] = {}
    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return Guard(
                False,
                f"{tests_dir.parent} is not a git checkout. In the exec mirror that loses nothing: "
                f"its tests/ IS `git archive HEAD tests`, so every file in it was tracked",
            )
        root = Path(os.fsdecode(top.stdout.strip()))
        if root.resolve() != tests_dir.parent.resolve():
            return Guard(
                False,
                f"{tests_dir} sits inside a repository rooted at {root}, not at its own checkout, "
                f"so that repository's index says nothing about what this tree ships",
            )
        for why, args in (
            ("untracked", ("--others", "--exclude-standard")),
            ("ignored", ("--others", "--ignored", "--exclude-standard")),
        ):
            proc = git("ls-files", "-z", *args, "--", ".")
            if proc.returncode != 0:
                return failed(proc)
            found.update((rel, why) for rel in _split(proc.stdout) if in_scope(rel))
        proc = git("ls-files", "-z", "--", ".")
        if proc.returncode != 0:
            return failed(proc)
        tracked = [rel for rel in _split(proc.stdout) if in_scope(rel)]
        if tracked:
            listing = b"".join(os.fsencode(rel) + b"\0" for rel in tracked)
            proc = git("check-attr", "-z", "--stdin", "export-ignore", stdin=listing)
            if proc.returncode != 0:
                return failed(proc)
            fields = proc.stdout.split(b"\0")  # path NUL attribute NUL value NUL, per path
            for i in range(0, len(fields) - 2, 3):
                if fields[i + 2] == b"set":
                    found[os.fsdecode(fields[i])] = "export-ignore"
    except (OSError, subprocess.SubprocessError) as exc:
        return Guard(False, f"git could not be run in {tests_dir}: {exc}")

    # Watch the path under each spelling a test can reach it by, so a symlinked parent cannot
    # turn a read into a miss.
    bases = {os.path.abspath(tests_dir), os.path.realpath(tests_dir)}
    watched = {
        os.path.normpath(os.path.join(base, rel)): (rel, why)
        for rel, why in found.items()
        for base in bases
    }
    if not found:
        return Guard(True, "nothing under tests/ is left out of `git archive HEAD tests`")
    return Guard(
        True, f"watching {len(found)} file(s) that `git archive HEAD tests` leaves out", watched
    )


_GUARD: Guard | None = None
# Module globals rather than attributes of _GUARD: the audit hook runs on every audited event in
# the process, so its fast path is two lookups and a return.
_WATCHED: dict[str, tuple[str, str]] = {}
_HITS: list[tuple[str, str]] = []
_HOOK_INSTALLED = False


def _audit(event: str, args: tuple) -> None:
    if event != "open" or not _WATCHED:
        return
    try:
        path = args[0]
        if isinstance(path, int):
            return
        hit = _WATCHED.get(os.path.abspath(os.fsdecode(path)))
        if hit is None:
            return
        what = effect(args[1], args[2])
        if what == "read":
            _HITS.append(hit)
        elif what == "truncate":
            # The test is writing this file, so whatever it reads back is its own output.
            for key in [k for k, v in _WATCHED.items() if v == hit]:
                del _WATCHED[key]
    except Exception:  # noqa: BLE001 -- an observer must never break the open it observes
        return


def describe(hits: list[tuple[str, str]], tests_name: str = "tests") -> str:
    head, *rest = hits
    more = f" (and {len(rest)} more below)" if rest else ""
    return "\n".join(
        [f"{tests_name}/{head[0]}: the exec mirror will not have this test input{more}"]
        + [f"  {tests_name}/{rel}: {WHY[why]}" for rel, why in hits]
        + [
            "orch-sync-mirror.sh ships tests/ as `git archive HEAD tests`, so this passes in a "
            "checkout and fails in ~/.codex/orchestrator-mirror after merge (tests/conftest.py)."
        ]
    )


def _charge(report: pytest.TestReport | pytest.CollectReport) -> None:
    """Fail the report for the phase in which a watched file was read."""
    if not _HITS:
        return
    hits = sorted(set(_HITS))
    _HITS.clear()
    text = describe(hits, TESTS_DIR.name)
    if report.failed:
        report.sections.append(("tracked test inputs", text))
    else:
        report.outcome = "failed"
        report.longrepr = text


def pytest_configure(config: pytest.Config) -> None:
    global _GUARD, _HOOK_INSTALLED
    _GUARD = arm(TESTS_DIR)
    _WATCHED.clear()
    _WATCHED.update(_GUARD.watched)
    _HITS.clear()
    if _WATCHED and not _HOOK_INSTALLED:
        sys.addaudithook(_audit)  # cannot be removed; pytest_unconfigure empties what it watches
        _HOOK_INSTALLED = True


def pytest_unconfigure(config: pytest.Config) -> None:
    _WATCHED.clear()
    _HITS.clear()


# tryfirst makes these the OUTERMOST wrappers, so the verdict they write is the one reported.
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    outcome = yield
    _charge(outcome.get_result())


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_make_collect_report(collector: pytest.Collector):
    outcome = yield
    _charge(outcome.get_result())


def pytest_terminal_summary(terminalreporter) -> None:
    if _GUARD is not None and (not _GUARD.armed or _GUARD.watched):
        state = "armed" if _GUARD.armed else "NOT CHECKED"
        terminalreporter.write_line(f"tracked test inputs: {state} -- {_GUARD.detail}")


@pytest.fixture
def tracked_inputs_guard() -> Guard | None:
    """The guard this session armed, for the test that asserts it armed where it should."""
    return _GUARD
