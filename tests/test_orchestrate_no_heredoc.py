"""orchestrate.sh feeds no here-document or here-string to anything, and its summary lines did not move.

THE HANG. On 2026-09-26 04:40Z the hourly tick blocked inside a child bash at `heredoc_write ->
write()`. That child was feeding the 677-byte pattern-miner summary to python's stdin as a
here-document. launchd never starts a second tick while one is running, so 141 hours went by with
zero cadence steps. Nothing FAILED, so `_mark_fail`, the backoff and the ALERT never fired.

THE MECHANISM, measured live before the kill. Homebrew bash 5.3 sends a here-document of up to
64 KiB through a pipe and writes all of it in one call before the reading command starts, so the
writer is the reader's own process. macOS starts a pipe at 512 bytes and grows it only while
system-wide pipe memory is below its limit. When the pipe cannot grow, any document over 512 bytes
blocks forever. A here-string goes through the same path. The tick's two other here-documents were
286 and 455 bytes, which is why only the 677-byte one hung. The CI runner is Linux, where a pipe
starts at 64 KiB, so a test that runs the tick could never reproduce this. The guard has to be
static.

So the rule is structural: no `<<` of any kind in the script. Each summary line now comes from a
`tick-line` subcommand of the module that owns the artifact. The pins below say exactly that, and
the rendering tests hold the lines to what the here-documents printed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import evidence_acquisition
import fleet_shapes
import paths
import pattern_miner

ORCHESTRATE = paths.REPO_ROOT / "orchestrate.sh"


def _logical_code_lines(text: str) -> list[str]:
    """Conservatively normalize executable lines before scanning for ``<<``."""
    normalized: list[str] = []
    quote: str | None = None
    only_whitespace = True
    index = 0
    while index < len(text):
        char = text[index]

        if quote is None and only_whitespace and char == "#":
            # A full-line comment ends at the physical newline. Its trailing backslash is data,
            # not a continuation, and quotes inside it do not affect the next physical line.
            newline = text.find("\n", index)
            if newline == -1:
                break
            normalized.append("\n")
            only_whitespace = True
            index = newline + 1
            continue

        if quote is None and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following == "\n":
                # Bash removes an unquoted backslash-newline before tokenization.
                index += 2
                continue
            # Consume an escaped quote as data so it cannot enter quote state.
            normalized.extend((char, following))
            only_whitespace = False
            index += 2
            continue

        if quote == '"' and char == "\\" and index + 1 < len(text):
            # Preserve escapes inside double quotes. Quoted text cannot become a redirection
            # operator, even though Bash removes a quoted backslash-newline before parsing.
            normalized.extend((char, text[index + 1]))
            only_whitespace = False
            index += 2
            continue

        normalized.append(char)
        if char == "\n":
            only_whitespace = True
        elif quote is None and char in {"'", '"'}:
            quote = char
            only_whitespace = False
        elif char == quote:
            quote = None
            only_whitespace = False
        elif not char.isspace():
            only_whitespace = False
        index += 1

    return "".join(normalized).splitlines()


def _code_lines() -> list[str]:
    """Every logical script line except full-line comments, which may describe the old shape."""
    text = ORCHESTRATE.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash"), f"{ORCHESTRATE} is not the tick script"
    return _logical_code_lines(text)


def _count(needle: str) -> int:
    return sum(line.count(needle) for line in _code_lines())


def test_no_here_document_is_fed_to_python_stdin() -> None:
    # The smallest fragment of the defect: python reading its PROGRAM from stdin, which is what a
    # here-document was there to feed. `-c` and `-u` do not match. Split, so it cannot match here.
    needle = "python3" + " - "
    assert _count(needle) == 0, (
        f"orchestrate.sh feeds python's stdin again ({_count(needle)} site(s) of {needle!r}). Print "
        "the line from a `tick-line` subcommand of the module that owns the artifact, as "
        "agent_switches.py, evidence_acquisition.py, pattern_miner.py and fleet_shapes.py do"
    )


def test_no_here_document_or_here_string_anywhere() -> None:
    # Wider than the python case: bash 5.3 pipes EVERY small here-document and here-string, whatever
    # command reads it, and the line-950 loop was fed a here-string the same way. `<<` covers
    # `<<'X'`, `<<X`, `<<-X` and `<<<`. Split, so it cannot match here.
    needle = "<" + "<"
    sites = [line.strip() for line in _code_lines() if needle in line]
    assert _count(needle) == 0, (
        "orchestrate.sh uses a here-document or here-string again, which bash 5.3 writes into a pipe "
        "that only the writing process will read; write the text to a file and redirect from it. "
        f"Sites: {sites}"
    )


def test_no_here_document_guard_joins_backslash_newline_spelling() -> None:
    """Bash removes the continuation before parsing, so a split operator is still prohibited."""
    needle = "<" + "<"
    prohibited = (
        "command <\\\n<EOF\nbody\nEOF\n",
        "command <\\\n\\\n<-EOF\nbody\nEOF\n",
        "printf %s 'start\n#'; cat <<EOF\nbody\nEOF\n",
    )
    prohibited += (
        "# explanatory comment " + "\\" + "\ncommand <<EOF\nbody\nEOF\n",
        "# comment with ' quote " + "\\" + "\ncommand <<<word\n",
        "true # inline comment " + "\\" + "\ncommand <<EOF\nbody\nEOF\n",
    )
    for spelling in prohibited:
        assert any(needle in line for line in _logical_code_lines(spelling)), spelling


def test_no_here_document_guard_preserves_quoted_and_escaped_newlines() -> None:
    """Continuation normalization must not invent a redirection Bash would keep quoted or split."""
    needle = "<" + "<"
    permitted = (
        "printf %s '<\\\n<'\n",
        'printf %s "<\\\n<"\n',
        "command <" + "\\\\" + "\n<EOF\n",
    )
    for spelling in permitted:
        assert all(needle not in line for line in _logical_code_lines(spelling)), spelling


def test_no_here_document_guard_keeps_hash_after_continuation_in_the_word() -> None:
    """A continued word's hash is data rather than a full-line comment."""
    assert _logical_code_lines("printf %s tag\\\n#value\n") == ["printf %s tag#value"]


def test_each_summary_line_comes_from_its_owning_module_exactly_once() -> None:
    script = ORCHESTRATE.read_text(encoding="utf-8")
    for module in ("evidence_acquisition.py", "pattern_miner.py", "fleet_shapes.py"):
        # `== 1`, not `in`: a pin with a second matching site stops proving the first one exists.
        assert script.count(module + '" tick-line') == 1, f"the tick prints {module}'s line once"
    for label in ("EVIDENCE-ACQ", "MINING", "MINING-ACTIONABLE", "SHAPES"):
        assert script.count(label + ": {") == 0, f"and holds no second rendering of {label}"


# ------------------------------------------------------------------ the lines did not move


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_evidence_acquisition_tick_line_is_the_here_documents_line(tmp_path: Path) -> None:
    shadow = _write(
        tmp_path / "shadow.json",
        {"state": "nothing_to_feed", "summary": "feedable 0 / capped 1 / candidates 0 / fed 0"},
    )
    live = _write(tmp_path / "live.json", {"state": "planned", "summary": "feedable 1", "live": 1})
    assert evidence_acquisition.render_tick_line(shadow) == (
        "  EVIDENCE-ACQ: nothing_to_feed — feedable 0 / capped 1 / candidates 0 / fed 0 [shadow]"
    )
    assert (
        evidence_acquisition.render_tick_line(live) == "  EVIDENCE-ACQ: planned — feedable 1 [LIVE]"
    )
    missing = tmp_path / "absent.json"
    assert evidence_acquisition.render_tick_line(missing) == (
        f"  EVIDENCE-ACQ: plan unreadable ([Errno 2] No such file or directory: '{missing}')"
    )
    # Valid JSON that is not an object used to end in a traceback; it now reads as unreadable.
    assert evidence_acquisition.render_tick_line(_write(tmp_path / "list.json", [1])) == (
        "  EVIDENCE-ACQ: plan unreadable (not a JSON object: list)"
    )
    assert evidence_acquisition.render_tick_line(_write(tmp_path / "null.json", None)) == (
        "  EVIDENCE-ACQ: plan unreadable (not a JSON object: NoneType)"
    )


def test_pattern_miner_tick_lines_are_the_here_documents_lines(tmp_path: Path) -> None:
    rejecting = _write(
        tmp_path / "status.json",
        {
            "mining_health": {
                "state": "rejecting",
                "summary": "accepted 0 / rejected 472 / excluded 4228 of 4700",
                "complete_episode_count": 0,
                "candidate_count": 0,
                "actionable": True,
                "detail": "472 of 4700 events rejected as malformed",
            }
        },
    )
    assert pattern_miner.render_tick_lines(rejecting) == [
        "  MINING: rejecting — accepted 0 / rejected 472 / excluded 4228 of 4700"
        " | episodes=0 candidates=0",
        "  MINING-ACTIONABLE: 472 of 4700 events rejected as malformed",
    ]
    quiet = _write(tmp_path / "quiet.json", {"other": 1})
    assert pattern_miner.render_tick_lines(quiet) == [
        "  MINING: unknown — no summary | episodes=? candidates=?"
    ]
    non_object = _write(tmp_path / "non-object.json", {"mining_health": [1]})
    assert pattern_miner.render_tick_lines(non_object) == [
        "  MINING: unknown — no summary | episodes=? candidates=?"
    ]
    bad = tmp_path / "bad.json"
    bad.write_text("{bad", encoding="utf-8")
    assert pattern_miner.render_tick_lines(bad) == [
        "  MINING: status unreadable (Expecting property name enclosed in double quotes: "
        "line 1 column 2 (char 1))"
    ]


def test_fleet_shapes_tick_line_is_the_here_documents_line(tmp_path: Path) -> None:
    _write(
        tmp_path / "fleet-shapes.json",
        {
            "window_days": 60,
            "counts": {
                "prs": 1120,
                "with_facts": 1120,
                "missing_facts": 0,
                "fetched_this_run": 79,
                "shapes": 296,
                "recurring": 58,
            },
        },
    )
    assert fleet_shapes.render_tick_line(tmp_path) == (
        "  SHAPES: 1120 merged agent PRs in 60d, facts for 1120 (0 missing, 79 fetched now), "
        "296 shapes, 58 recurring"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert fleet_shapes.render_tick_line(empty) == (
        "  SHAPES: artifact unreadable ([Errno 2] No such file or directory: "
        f"'{empty / 'fleet-shapes.json'}')"
    )
    _write(tmp_path / "fleet-shapes.json", None)
    assert fleet_shapes.render_tick_line(tmp_path) == (
        "  SHAPES: artifact unreadable (not a JSON object: NoneType)"
    )
    _write(tmp_path / "fleet-shapes.json", {"counts": [1]})
    assert fleet_shapes.render_tick_line(tmp_path) == (
        "  SHAPES: artifact unreadable (counts is not a JSON object: list)"
    )


def test_keepalive_shadow_loop_processes_25_prs_without_sharing_stdin(tmp_path: Path) -> None:
    """Replay the committed loop: each worker sees EOF and cannot consume the remaining PR list."""
    script = ORCHESTRATE.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^      n=0\n(?P<loop>      while IFS= read -r pr; do\n.*?^      done < \"\$keepalive_shadow_prs\")",
        script,
    )
    assert match, "keepalive shadow loop remains recognizable for the stdin-isolation replay"
    loop = match.group("loop")

    prs = tmp_path / "prs.txt"
    prs.write_text("".join(f"stranske/repo#{n}\n" for n in range(1, 31)), encoding="utf-8")
    captures = tmp_path / "stdin-bytes.txt"
    replay = "\n".join(
        [
            "python3() { bytes=$(wc -c | tr -d ' '); printf '%s\\n' \"$bytes\" >> \"$captures\"; }",
            "_mark_success() { :; }",
            "n=0",
            loop,
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", replay],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "ORCH": str(tmp_path),
            "keepalive_shadow_prs": str(prs),
            "captures": str(captures),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert captures.read_text(encoding="utf-8").splitlines() == ["0"] * 25


def test_the_subcommands_print_those_lines(tmp_path: Path) -> None:
    """The tick calls the CLIs, so the wiring is checked through them, once per module."""
    plan = _write(tmp_path / "plan.json", {"state": "no_candidates", "summary": "feedable 0"})
    status = _write(tmp_path / "status.json", {"mining_health": {"state": "mining"}})
    _write(tmp_path / "fleet-shapes.json", {"window_days": 1, "counts": {"prs": 2}})
    calls = {
        "evidence_acquisition.py": (
            ["tick-line", "--plan", str(plan)],
            [evidence_acquisition.render_tick_line(plan)],
        ),
        "pattern_miner.py": (
            ["tick-line", "--status", str(status)],
            [*pattern_miner.render_tick_lines(status)],
        ),
        "fleet_shapes.py": (
            ["tick-line", "--state-dir", str(tmp_path)],
            [fleet_shapes.render_tick_line(tmp_path)],
        ),
    }
    for module, (argv, expected) in calls.items():
        proc = subprocess.run(
            [sys.executable, str(paths.MODULE_DIR / module), *argv],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, f"{module} tick-line exited {proc.returncode}: {proc.stderr}"
        assert proc.stdout.splitlines() == expected, (module, proc.stdout)
