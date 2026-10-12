"""A worktree_git refusal leaves a durable row, and the weekly sweep counts it (2026-10-11).

THE GAP. #523's guard refused on stderr only. The tick's and the dispatch wrapper's logs captured
the line, but nothing read those logs for it, so a refusal (an agent's worktree disagreeing with
where the tool put it) was visible only to someone already looking.

THE RULE. `worktree_git.run` appends one JSON row per refusal to
`$ORCH_STATE_DIR/worktree-git-refusals.jsonl` (time, worktree, the pointer file, the reason, the
caller), best-effort: a failed write never changes the refusal. `switch_review` prints the count for
its window and the latest worktrees, FYI only, with the zero case stated, and also counts the
`skip-git` lines `~/.codex/bin/lane-worktree-gc.sh` writes to its log. These tests assert refusal,
rows and argv/env only; nothing here configures git to run a program.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

import pushed_branches
import switch_review
import worktree_git


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    monkeypatch.delenv(worktree_git.DISABLE_ENV, raising=False)
    monkeypatch.delenv(worktree_git.REFUSAL_LOG_ENV, raising=False)
    root = Path(os.path.realpath(tmp_path))
    repos, wts, state = root / "repos", root / "worktrees", root / "state"
    clone = repos / "o__r"
    _git("init", "-q", str(clone))
    _git(
        "-C",
        str(clone),
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    wts.mkdir()
    wt = wts / "o__r__1__opener"
    _git("-C", str(clone), "worktree", "add", "-q", "-b", "b1", str(wt))
    import provision

    monkeypatch.setattr(provision, "REPOS_DIR", repos)
    monkeypatch.setattr(provision, "WORKTREES_DIR", wts)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    private = clone / ".git" / "worktrees" / wt.name
    return {
        "root": root,
        "wt": wt,
        "private": private,
        "log": state / "worktree-git-refusals.jsonl",
    }


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


TAMPERS = {
    "commondir": lambda lay: (lay["private"] / "commondir").write_text(str(lay["root"]) + "\n"),
    "gitdir": lambda lay: (lay["private"] / "gitdir").write_text(
        str(lay["root"] / "x/.git") + "\n"
    ),
    "config.worktree": lambda lay: (lay["private"] / "config.worktree").write_text(""),
    ".git": lambda lay: (lay["wt"] / ".git").write_text(f"gitdir: {lay['root'] / 'o/.git'}\n"),
}


@pytest.mark.parametrize("named", sorted(TAMPERS))
def test_each_refusal_appends_one_row_naming_its_file(layout, named):
    TAMPERS[named](layout)
    result = worktree_git.run(layout["wt"], ["status", "--porcelain"])
    assert result.returncode == worktree_git.REFUSED_RC
    rows = _rows(layout["log"])
    assert len(rows) == 1, rows
    row = rows[0]
    assert Path(row["file"]).name == named, row
    assert row["worktree"] == str(layout["wt"])
    assert row["reason"] in result.stderr and "re-provision" in row["reason"]
    assert row["git"] == "status"
    assert type(row["ts"]) is int and row["utc"].endswith("Z")


def test_the_row_names_the_calling_function(layout):
    TAMPERS["commondir"](layout)
    assert pushed_branches._git(layout["wt"], "rev-parse", "HEAD").returncode == 97
    assert _rows(layout["log"])[0]["caller"] == "pushed_branches._git"


def test_a_failed_write_never_changes_the_refusal(layout, monkeypatch, capsys):
    TAMPERS["commondir"](layout)
    unwritable = layout["root"] / "a-directory"
    unwritable.mkdir()
    monkeypatch.setenv(worktree_git.REFUSAL_LOG_ENV, str(unwritable))
    calls = []
    monkeypatch.setattr(worktree_git.subprocess, "run", lambda *a, **k: calls.append(a))
    result = worktree_git.run(layout["wt"], ["status"])
    assert result.returncode == worktree_git.REFUSED_RC
    assert result.stderr.startswith(worktree_git.REFUSAL_PREFIX)
    assert calls == []
    assert "refusal not recorded" in capsys.readouterr().err


def test_a_passing_check_and_the_kill_switch_record_nothing(layout, monkeypatch):
    assert worktree_git.run(layout["wt"], ["rev-parse", "HEAD"]).returncode == 0
    TAMPERS["commondir"](layout)
    monkeypatch.setenv(worktree_git.DISABLE_ENV, "1")
    argv, env = worktree_git.plan(layout["wt"], ["status"])
    assert env is None and argv[:2] == ["git", "-C"]
    assert not layout["log"].exists()


def test_the_weekly_sweep_counts_what_the_guard_wrote(layout, tmp_path):
    TAMPERS["commondir"](layout)
    worktree_git.run(layout["wt"], ["status"])
    worktree_git.run(layout["wt"], ["log"])
    section = switch_review.worktree_git_refusals(gc_log=tmp_path / "no-gc.log")
    assert section["refusals"]["status"] == "ok"
    assert section["refusals"]["count"] == 2
    assert section["refusals"]["latest"] == [str(layout["wt"])]
    text = "\n".join(switch_review.format_worktree_git_refusals(section))
    assert f"worktree-git refusals: 2 in 7d (latest: {layout['wt']})" in text
    assert "re-provision" in text


def test_the_zero_case_prints_and_differs_from_unreadable(tmp_path):
    gc = tmp_path / "gc.log"
    gc.write_text("fatal: Unable to read current working directory\n")
    zero = switch_review.worktree_git_refusals(log_path=tmp_path / "absent.jsonl", gc_log=gc)
    text = "\n".join(switch_review.format_worktree_git_refusals(zero))
    assert "worktree-git refusals: 0 in 7d (no refusal ever recorded at" in text
    assert "lane-worktree-gc pointer skips: 0 in 7d" in text
    unreadable = switch_review.worktree_git_refusals(log_path=tmp_path, gc_log=tmp_path / "none")
    assert unreadable["refusals"]["status"] == "unknown"
    text = "\n".join(switch_review.format_worktree_git_refusals(unreadable))
    assert "worktree-git refusals: NOT MEASURED" in text
    assert "lane-worktree-gc pointer skips: NOT MEASURED" in text


def test_the_window_and_the_gc_skip_lines(tmp_path):
    now = 1_800_000_000  # 2027-01-15T08:00:00Z
    log = tmp_path / "r.jsonl"
    rows = [
        {"ts": now - 10, "worktree": "/w/b"},
        {"ts": now - 20, "worktree": "/w/a"},
        {"ts": now - 30, "worktree": "/w/b"},
        {"ts": now - 8 * 86400, "worktree": "/w/old"},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{broken\n")
    gc = tmp_path / "gc.log"
    gc.write_text(
        "2027-01-15T07:00:00Z lane-worktree-gc skip-git worktree=/lane/wt-1 reason=named x\n"
        "2027-01-01T07:00:00Z lane-worktree-gc skip-git worktree=/lane/wt-old reason=y\n"
        "fatal: noise\n"
    )
    section = switch_review.worktree_git_refusals(now=now, log_path=log, gc_log=gc)
    assert section["refusals"]["count"] == 3
    assert section["refusals"]["latest"] == ["/w/b", "/w/a"]
    assert section["refusals"]["malformed"] == 1
    assert section["gc_skips"] == {
        "path": str(gc),
        "count": 1,
        "latest": ["/lane/wt-1"],
        "status": "ok",
    }


def test_the_section_is_in_the_report_and_never_raises_a_decision():
    # Wiring pins: the smallest fragment absent if the wiring is removed, each matched once.
    review_src = inspect.getsource(switch_review.review)
    assert review_src.count('"worktree_git_refusals": ' + "worktree_git_refusals()") == 1
    assert "worktree_git_refusals" not in review_src.split('"raise_count":')[1]
    report_src = inspect.getsource(switch_review.format_report)
    assert report_src.count("format_" + "worktree_git_refusals(refusals)") == 1
