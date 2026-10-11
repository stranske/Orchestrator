"""Production-generated Seatbelt regex regressions, runnable with pytest or stdlib unittest.

The string-layer checks do not establish native parsing or enforcement. On supported macOS each
case also executes the production profile in a disposable directory; elsewhere native acceptance
is explicitly UNKNOWN. No agent, installed mirror or research-program directory is used.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import adapters


class SeatbeltRegexQuotingTests(unittest.TestCase):
    def check_root(self, name):
        with tempfile.TemporaryDirectory(prefix="seatbelt-regression-") as directory:
            root = Path(directory)
            if sys.platform == "darwin" and Path(adapters.SANDBOX_EXEC).is_file():
                # Native proof runs first: a production regex-reader regression must fail in
                # sandbox-exec, rather than only in the supplemental expected-string assertion.
                _assert_native_profile_enforces_unusual_allowed_roots(root, name)
            else:
                print(
                    "native Seatbelt parser/enforcement acceptance UNKNOWN: requires macOS sandbox-exec"
                )
            _assert_generated_regex_literal_preserves_the_root_and_git_boundary(root, name)

    def test_quote(self):
        self.check_root('odd"root')

    def test_backslash(self):
        self.check_root(r"back\slash")

    def test_regex_metacharacters(self):
        self.check_root("meta.[x]+(a){2}^$|?*")

    def test_combined(self):
        self.check_root('mixed"back\\slash.[x]+(a){2}^$|?* space')
        self.check_root("plain-root")


def _assert_generated_regex_literal_preserves_the_root_and_git_boundary(tmp_path, name):
    protected = tmp_path / "protected-tree"
    root = protected / name
    root.mkdir(parents=True)
    protect, allow = adapters.write_boundary_roots([protected], [], root, agent="vibe")
    profile = adapters.seatbelt_write_profile(protect, allow)

    # JSON models only the quote/backslash string layer used here, NOT a Seatbelt parser.
    # Require complete regex rules: an embedded bare quote must not truncate the literal.
    literals = re.findall(r'\(deny file-write\* \(regex ("(?:\\.|[^"\\])*")\)\)', profile)
    assert len(literals) == 1, profile
    pattern = json.loads(literals[0])
    assert pattern == "^" + re.escape(str(root.resolve())) + r"/(.*/)?\.git(/|$)"
    regex = re.compile(pattern)
    for suffix in ("/.git", "/.git/config", "/nested/.git/hooks/pre-commit"):
        assert regex.search(str(root.resolve()) + suffix)
    for path in (
        str(root.resolve()) + "/source.py",
        str(root.resolve()) + "/nested/.gitignore",
        str(root.resolve()) + "/.git-other/config",
        str(root.resolve()) + "-sibling/.git/config",
        str(root.resolve()).replace(name, "different-root") + "/.git/config",
    ):
        assert not regex.search(path), path

    deny = "(deny file-write* (subpath " + json.dumps(protect[0]) + "))"
    grant = "(allow file-write* (subpath " + json.dumps(allow[0]) + "))"
    git_deny = "(deny file-write* (regex " + literals[0] + "))"
    assert profile == "(version 1)(allow default)" + deny + grant + git_deny


def _assert_native_profile_enforces_unusual_allowed_roots(tmp_path, name):
    protected = tmp_path.resolve() / ("protected-" + name)
    root = protected / name
    allowed = [
        root / "source.py",
        root / "nested" / "source.py",
        root / ".gitignore",
        root / ".git-other" / "config",
        # Outside the protected tree: an overbroad/underescaped root regex must not deny this.
        tmp_path.resolve() / ("protected-" + name + "-sibling") / name / ".git" / "config",
    ]
    denied = [
        protected / "driver.py",
        protected / (name + "-sibling") / "source.py",
        root / ".git" / "config",
        root / "nested" / ".git" / "hooks" / "pre-commit",
    ]
    for path in [*allowed, *denied]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original")
    protect, allow = adapters.write_boundary_roots([protected], [], root, agent="vibe")
    profile = adapters.seatbelt_write_profile(protect, allow)
    # Execute the production profile, not a hand-written equivalent. All attempts stay in tmp_path.
    probe = r"""
import json, pathlib, sys
allowed, denied = json.loads(sys.argv[1])
for name in allowed:
    path = pathlib.Path(name)
    path.write_text("changed")
    path.with_name(path.name + ".new").write_text("created")
    moved = path.with_name(path.name + ".moved")
    path.rename(moved)
    moved.rename(path)
for name in denied:
    path = pathlib.Path(name)
    for target in (path, path.with_name(path.name + ".new")):
        try:
            target.write_text("forbidden")
        except PermissionError:
            continue
        raise AssertionError("write allowed: " + str(target))
    try:
        path.rename(path.with_name(path.name + ".moved"))
    except PermissionError:
        pass
    else:
        raise AssertionError("rename allowed: " + str(path))
    try:
        path.unlink()
    except PermissionError:
        pass
    else:
        raise AssertionError("unlink allowed: " + str(path))
print("native Seatbelt probe: OK")
"""
    proc = subprocess.run(
        [
            adapters.SANDBOX_EXEC,
            "-p",
            profile,
            sys.executable,
            "-I",
            "-c",
            probe,
            json.dumps([[str(p) for p in allowed], [str(p) for p in denied]]),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert proc.stdout.strip() == "native Seatbelt probe: OK"
    print(proc.stdout.strip())
    for path in allowed:
        assert path.read_text() == "changed"
        assert path.with_name(path.name + ".new").read_text() == "created"
    for path in denied:
        assert path.read_text() == "original"
        assert not path.with_name(path.name + ".new").exists()
        assert not path.with_name(path.name + ".moved").exists()
