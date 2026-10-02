from __future__ import annotations

import subprocess
from pathlib import Path

import merge_guard

ROOT = Path(__file__).resolve().parents[1]


def _open_meta(target: str) -> dict:
    return {
        "target": target,
        "labels": [],
        "title": "ready",
        "state": "OPEN",
        "is_draft": False,
    }


def test_active_merge_requires_expected_head() -> None:
    result = merge_guard.guarded_merge("o/r#5", confirm_merge=True, metadata_fn=_open_meta)

    assert result["blocked"] is True
    assert result["merge_executed"] is False
    assert "--expected-head" in result["reason"]


def test_final_preflight_blocks_mutation_when_head_or_threads_change() -> None:
    snapshots = iter(
        [
            {"blocked": False, "head": "abc123"},
            {"blocked": True, "reason": "1 active non-outdated review thread(s) remain"},
        ]
    )
    merge_calls: list[list[str]] = []

    result = merge_guard.guarded_merge(
        "o/r#5",
        expected_head="abc123",
        confirm_merge=True,
        metadata_fn=_open_meta,
        preflight_fn=lambda target, expected_head: next(snapshots),
        merge_fn=lambda cmd, **kwargs: (
            merge_calls.append(cmd)
            or subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")
        ),
    )

    assert result["blocked"] is True
    assert result["merge_executed"] is False
    assert "active non-outdated review thread" in result["reason"]
    assert merge_calls == []


def test_malformed_initial_preflight_cannot_authorize_merge() -> None:
    result = merge_guard.guarded_merge(
        "o/r#5",
        expected_head="abc123",
        confirm_merge=True,
        metadata_fn=_open_meta,
        preflight_fn=lambda target, expected_head: {"head": expected_head},
    )

    assert result["blocked"] is True
    assert result["merge_executed"] is False
    assert result["reason"] == "exact-head preflight returned a malformed result"


def test_terminal_merge_contract_is_repo_wide() -> None:
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "src/merge_guard.py" in text, name
        assert "Direct `gh pr merge`" in text, name
    operator_doc = (ROOT / "ORCHESTRATOR.md").read_text(encoding="utf-8")
    assert "--expected-head" in operator_doc
    assert "seven-minute exact-head review floor" in operator_doc


def test_merge_command_pins_the_observed_head() -> None:
    cmd = merge_guard.build_merge_cmd("o/r#5", expected_head="abc123")

    assert cmd[-2:] == ["--match-head-commit", "abc123"]
