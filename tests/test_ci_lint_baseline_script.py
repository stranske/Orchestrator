"""The baseline re-measure must be able to run, and must count what mypy calls errors.

THE DEFECT (2026-10-11). The dev-tool sync wrote new pins to main (ruff 0.16.10, black 26.10.0, mypy
2.4.0), and `test_ci_gate_config.test_baseline_was_measured_with_the_pinned_versions` went red, as
designed, until `scripts/ci_lint_baseline.py` re-measured. The script could not: `_scratch_copy`
decoded the `git archive` tar stream as UTF-8, and the tree tracks gzip evidence since 2026-10-07,
so it raised `UnicodeDecodeError` before ruff ran. The gate's only drain was blocked by the tree it
measures. Once it ran, mypy's clean pass (`Success: no issues found in 122 source files`) read
`1 errors <-- DEADLOCK`: with no `Found N errors` line the script counted the distinct codes of every
bracketed line, and six `annotation-unchecked` notes share one code.
"""

from __future__ import annotations

import gzip
import importlib.util
import subprocess
from pathlib import Path

import paths

SCRIPT = paths.REPO_ROOT / "scripts" / "ci_lint_baseline.py"


def load():
    spec = importlib.util.spec_from_file_location("ci_lint_baseline_under_test", SCRIPT)
    assert spec and spec.loader, SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


def test_the_scratch_copy_carries_binary_files(tmp_path, monkeypatch):
    """A tracked gzip file travels through the archive byte for byte, beside the text files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    payload = gzip.compress(b"evidence " * 500, mtime=0)
    assert any(byte >= 0x80 for byte in payload), "the fixture must not be valid ASCII"
    (repo / "evidence.tar.gz").write_bytes(payload)
    (repo / "module.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    script = load()
    monkeypatch.setattr(script, "REPO", repo)
    with script._scratch_copy() as scratch:
        out = Path(scratch)
        assert (out / "evidence.tar.gz").read_bytes() == payload
        assert (out / "module.py").read_text(encoding="utf-8") == "x = 1\n"


# mypy 2.4.0's own output over this tree on 2026-10-11, shortened to three of its six notes.
CLEAN = (
    "src/feedback.py:6245: note: By default the bodies of untyped functions are not checked, "
    "consider using --check-untyped-defs  [annotation-unchecked]\n"
    "src/capability_propensity.py:5073: note: By default the bodies of untyped functions are not "
    "checked, consider using --check-untyped-defs  [annotation-unchecked]\n"
    "src/exp_abcd.py:2876: note: By default the bodies of untyped functions are not checked, "
    "consider using --check-untyped-defs  [annotation-unchecked]\n"
    "Success: no issues found in 122 source files\n"
)
ERRORS = (
    'src/a.py:3: error: Argument 1 to "f" has incompatible type "str"; expected "int"  '
    "[arg-type]\n"
    "src/a.py:9: note: By default the bodies of untyped functions are not checked, consider "
    "using --check-untyped-defs  [annotation-unchecked]\n"
    'src/a.py:12: error: Value of type "int" is not indexable  [index]\n'
    "src/a.py:14: error: Argument 2 has incompatible type  [arg-type]\n"
)


def test_a_clean_run_with_notes_is_zero_errors():
    assert load().mypy_counts(CLEAN) == (0, {}, None)


def test_errors_are_counted_and_notes_are_not():
    script = load()
    with_summary = ERRORS + "Found 3 errors in 1 file (checked 2 source files)\n"
    assert script.mypy_counts(with_summary) == (3, {"arg-type": 2, "index": 1}, 1)
    # No summary line (a truncated log): the error lines are the count, never the codes.
    assert script.mypy_counts(ERRORS) == (3, {"arg-type": 2, "index": 1}, None)
