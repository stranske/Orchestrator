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

import inspect
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import improvement_log as il

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


@pytest.fixture(autouse=True)
def isolated_improvement_runtime(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_IMPROVEMENT_LOG", str(runtime / il.LOG_NAME))

    attempted: list[tuple] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("in-process improvement_log tests must not spawn processes or sockets")

    if request.node.name != "test_accessor_reports_a_named_absence_to_a_caller":
        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield runtime
    assert attempted == [], f"unexpected external calls: {attempted}"


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


SAMPLE_LOG = """# Instance improvement log
Preamble mention of thompson before any heading.

## 🟢 4. Durable repo-knowledge memory

Cached pattern details.


## ✅ 7. Telemetry integrity

### 7a. Cost-aware scoring

Thompson sampling was wired here and is DONE.

### 7b. Thompson sampling flag

Held OFF behind a switch.

## ⚪ 9. Thompson routing follow-up

Still open.
"""


def test_log_path_prefers_direct_env_then_local_runtime(
    isolated_improvement_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct_target = isolated_improvement_runtime / "custom" / "backlog.md"
    monkeypatch.setenv(il.ENV_DIRECT, str(direct_target))
    assert il.log_path() == direct_target

    monkeypatch.delenv(il.ENV_DIRECT, raising=False)
    expected_runtime_log = isolated_improvement_runtime / il.LOG_NAME
    assert il.log_path() == expected_runtime_log

    note = il.absence_note(expected_runtime_log)
    assert str(expected_runtime_log) in note
    assert il.ENV_DIRECT in note and il.ENV_RUNTIME in note


def test_parse_sections_and_search_resolve_deepest_owning_heading_with_context_and_limit(
    isolated_improvement_runtime: Path,
) -> None:
    missing = isolated_improvement_runtime / "absent.md"
    absent_rep = il.search("thompson", path=missing)
    assert absent_rep["present"] is False
    assert str(missing) in il.render_search(absent_rep)

    log = isolated_improvement_runtime / il.LOG_NAME
    log.write_text(SAMPLE_LOG, encoding="utf-8")

    empty_rep = il.search("never-mentioned-token", path=log)
    assert empty_rep["present"] is True
    assert empty_rep["matches"] == []
    empty_rendered = il.render_search(empty_rep)
    assert "NO MATCHING ITEMS" in empty_rendered
    assert "PROBLEM:" not in empty_rendered

    hit_rep = il.search("thompson", path=log, limit=3, context=1)
    assert hit_rep["present"] is True
    assert hit_rep["total_matches"] == 4
    assert hit_rep["truncated"] is True
    assert len(hit_rep["matches"]) == 3
    assert hit_rep["matches"][0]["section"] == "(before the first heading)"
    assert hit_rep["matches"][0]["section_line"] == 0
    assert hit_rep["matches"][0]["item"] is None
    assert hit_rep["matches"][1]["section"] == "7a. Cost-aware scoring"
    assert hit_rep["matches"][1]["item"] is None
    assert hit_rep["matches"][1]["context"]

    item_hit = il.search("Cached pattern", path=log)
    assert item_hit["matches"][0]["item"] == "4"
    rendered_item = il.render_search(item_hit)
    assert "item 4)" in rendered_item

    rendered_hits = il.render_search(hit_rep)
    assert "4 match(es), showing the first 3" in rendered_hits
    assert "## (before the first heading)   (line 0)" in rendered_hits
    assert "## 7a. Cost-aware scoring" in rendered_hits
    assert "      | " in rendered_hits


def test_find_section_and_append_note_place_status_inside_section_with_backup(
    isolated_improvement_runtime: Path,
) -> None:
    missing = isolated_improvement_runtime / "missing.md"
    absent = il.append_note("4", "note", path=missing, today="2026-11-07")
    assert absent["absent"] is True
    assert str(missing) in il.render_append(absent)
    assert not missing.exists()

    log = isolated_improvement_runtime / il.LOG_NAME
    log.write_text(SAMPLE_LOG, encoding="utf-8")

    blank = il.append_note("4", "   ", path=log, today="2026-11-07")
    assert blank["ok"] is False and blank["absent"] is False
    assert "improvement_log.py search" in il.render_append(blank)

    ambiguous = il.append_note("Thompson", "status update", path=log, today="2026-11-07")
    assert ambiguous["ok"] is False
    assert len(ambiguous["candidates"]) == 2
    ambig_text = il.render_append(ambiguous)
    assert "REFUSED" in ambig_text and "candidates" in ambig_text

    unknown = il.append_note("#99", "status update", path=log, today="2026-11-07")
    assert unknown["ok"] is False and unknown["candidates"] == []
    assert "no section matches for item number 99" in unknown["reason"]

    appended = il.append_note("#04", "shipped in closer round", path=log, today="2026-11-07")
    assert appended["ok"] is True
    assert appended["entry"] == "- **STATUS 2026-11-07:** shipped in closer round"
    rendered_ok = il.render_append(appended)
    assert "appended to" in rendered_ok and "backup:" in rendered_ok

    updated = log.read_text(encoding="utf-8")
    before_sec7, sep, after_sec7 = updated.partition("## ✅ 7. Telemetry integrity")
    assert sep
    assert "- **STATUS 2026-11-07:** shipped in closer round" in before_sec7
    assert "shipped in closer round" not in after_sec7
    backup = Path(appended["backup"])
    assert backup.read_text(encoding="utf-8") == SAMPLE_LOG


def test_append_note_cleans_up_tempfile_when_atomic_replace_fails(
    isolated_improvement_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = isolated_improvement_runtime / il.LOG_NAME
    log.write_text(SAMPLE_LOG, encoding="utf-8")

    def fail_replace(_src: str, _dst: Path) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(il.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        il.append_note("Cost-aware", "should rollback", path=log, today="2026-11-07")

    assert log.read_text(encoding="utf-8") == SAMPLE_LOG
    leftover_tmps = list(isolated_improvement_runtime.glob(".improvement-log-*.tmp"))
    assert leftover_tmps == []


def test_main_subcommands_cover_help_path_search_and_append_exit_codes(
    isolated_improvement_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = isolated_improvement_runtime / il.LOG_NAME

    assert il.main([]) == il.EXIT_OK
    assert "usage:" in capsys.readouterr().out.lower()

    # Absent log across path / search / append.
    assert il.main(["path"]) == il.EXIT_ABSENT
    assert "present: False" in capsys.readouterr().out
    assert il.main(["path", "--json"]) == il.EXIT_ABSENT
    assert json.loads(capsys.readouterr().out)["present"] is False
    assert il.main(["search", "thompson"]) == il.EXIT_ABSENT
    capsys.readouterr()
    assert il.main(["append", "4", "note"]) == il.EXIT_ABSENT
    capsys.readouterr()

    # Present log across path / search / append.
    log.write_text(SAMPLE_LOG, encoding="utf-8")
    assert il.main(["path"]) == il.EXIT_OK
    path_out = capsys.readouterr().out
    assert "present: True" in path_out and "PROBLEM:" not in path_out

    assert il.main(["search", "no-such-needle"]) == il.EXIT_NO_MATCH
    assert "NO MATCHING ITEMS" in capsys.readouterr().out

    assert il.main(["search", "thompson", "--json"]) == il.EXIT_OK
    assert json.loads(capsys.readouterr().out)["total_matches"] == 4

    assert il.main(["append", "Thompson", "ambiguous"]) == il.EXIT_REFUSED
    assert "REFUSED" in capsys.readouterr().out

    assert il.main(["append", "4", "completed", "--json"]) == il.EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


def main() -> int:
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if inspect.signature(fn).parameters:
            print(f"  skip {name} — needs pytest fixtures")
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
