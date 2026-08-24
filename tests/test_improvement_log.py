#!/usr/bin/env python3
"""test_improvement_log.py — the TREE invariants behind the improvement-log accessor.

`improvement_log.py --selftest` covers the module's own behaviour (search, append, the named
absence) against a synthetic log in a tempdir. It deliberately never touches this instance's real
log, which is machine-local and absent on any other machine.

What it CANNOT cover is the three facts about the repository that made the defect possible, and each
one rots silently:

  1. the tracked `IMPROVEMENT_BACKLOG.md` is a POINTER, and stays one. The whole change is undone the
     moment someone appends evidence to it: the 481 KB machine-local log starts becoming committed,
     which is exactly what the tool-vs-evidence split forbids. Prose asking nicely is what failed
     before — `ADDING_CAPABILITIES.md` opens with that lesson — so the size limit is a test.
  2. the pointer NAMES the accessor. A pointer that does not say how to reach the log leaves a
     worktree exactly as blind as a gitignored file did.
  3. `CLAUDE.md` §0 step 3 and §5 name the ACCESSOR, not a bare path. Those two rules are the reason
     the log exists; re-writing either back into a path re-breaks it for every worker in a worktree.

These are cheap, and they are the only thing that survives the next session.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Repo-root files, resolved through the shared rule rather than a local `parent.parent`:
# these tests live in `tests/` while the things they assert on live at the checkout root.
import paths

HERE = paths.REPO_ROOT
POINTER = HERE / "IMPROVEMENT_BACKLOG.md"
CLAUDE_MD = HERE / "CLAUDE.md"
# TWO NAMES, because there are two questions. The docs cite the accessor the way a reader types it
# (`improvement_log.py ...`), while INVOKING it needs the real path — the modules moved under src/
# and the tests no longer share their directory. Conflating them made the doc assertions look for a
# machine-specific absolute path inside CLAUDE.md.
ACCESSOR = "improvement_log.py"
ACCESSOR_PATH = str(paths.MODULE_DIR / ACCESSOR)

# A pointer is a paragraph and three commands. The real log is ~480 KB / 6,000 lines, so anything in
# between is someone having started to use this file as the log. The gap between the two is three
# orders of magnitude wide: this bound cannot be tripped by ordinary prose edits.
POINTER_MAX_BYTES = 8_192
POINTER_MAX_SECTIONS = 4


def test_tracked_pointer_stays_a_pointer_and_never_becomes_the_log():
    """The 481 KB of machine-local evidence must never arrive at this tracked path."""
    assert POINTER.is_file(), (
        f"{POINTER.name} must be tracked in the tree — it is what makes the "
        f"machine-local log discoverable from a worktree"
    )
    size = POINTER.stat().st_size
    assert size <= POINTER_MAX_BYTES, (
        f"{POINTER.name} is {size} bytes, over the {POINTER_MAX_BYTES}-byte pointer limit. This file "
        f"is a POINTER; the log itself is machine-local evidence and must not be committed. Append "
        f'with `python3 {ACCESSOR} append <item-ref> "<note>"` instead.'
    )
    sections = len(re.findall(r"(?m)^##\s", POINTER.read_text(encoding="utf-8")))
    assert (
        sections <= POINTER_MAX_SECTIONS
    ), f"{POINTER.name} has {sections} `##` sections — it is turning into the log it points at."


def test_tracked_pointer_names_the_accessor_and_both_rules():
    """A pointer that does not name the accessor leaves a worktree as blind as before."""
    text = POINTER.read_text(encoding="utf-8")
    assert (
        ACCESSOR in text
    ), f"the pointer must name {ACCESSOR} — it is the only way to reach the log"
    for cmd in ("search", "append"):
        assert f"{ACCESSOR} {cmd}" in text, f"the pointer must show `{ACCESSOR} {cmd}`"
    assert "ORCH_LOCAL_RUNTIME" in text, "the pointer must say WHERE the log lives"


def test_claude_md_rules_name_the_accessor_not_a_bare_path():
    """§0 step 3 and §5 are the two rules the accessor exists to make followable."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    # §0 step 3 — the dedup check.
    step3 = [
        ln
        for ln in text.splitlines()
        if ln.lstrip().startswith("3. ") and "improvement log" in ln.lower()
    ]
    assert step3, "CLAUDE.md §0 step 3 must tell the reader to search the improvement log"
    dedup = text.split("## 0. Dedup-before-develop", 1)[-1].split("## 1. Editing", 1)[0]
    assert f"{ACCESSOR} search" in dedup, (
        f"CLAUDE.md §0 must name `{ACCESSOR} search` — a bare path is unreadable from a worktree, "
        f"which is what made this mandatory step unfollowable"
    )
    # §5 — the status note.
    keep_true = text.split("## 5. Keep the docs true", 1)[-1]
    assert (
        f"{ACCESSOR} append" in keep_true
    ), f"CLAUDE.md §5 must name `{ACCESSOR} append` rather than telling the reader to edit a file"


def test_accessor_reports_a_named_absence_to_a_caller():
    """The caller-facing contract, asserted on what a CALLER receives: text and exit code.

    Duplicated from the selftest on purpose and kept minimal: this is the assertion that the
    founding defect — an empty result indistinguishable from a missing file — stays fixed, and it
    must hold under `pytest` (which is what CI actually runs) and not only under `--selftest`.
    Points at a path that cannot exist, so it never reads this instance's real log.
    """
    missing = HERE / "no-such-dir-for-tests" / "IMPROVEMENT_BACKLOG.md"
    assert not missing.exists()
    proc = subprocess.run(
        [sys.executable, ACCESSOR_PATH, "search", "anything"],
        capture_output=True,
        text=True,
        cwd=str(HERE),
        env={"PATH": "/usr/bin:/bin", "HOME": str(HERE), "ORCH_IMPROVEMENT_LOG": str(missing)},
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"an absent log must exit 2, not {proc.returncode}: {out[:300]}"
    assert str(missing) in out, "the absence must name the path it looked for"
    assert "ORCH_LOCAL_RUNTIME" in out, "the absence must name the env var that controls the path"
    assert out.strip(), "an absent log must never produce an empty result"


def main() -> int:
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL {name} — {exc}")
    print(f"improvement-log tree invariants: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
