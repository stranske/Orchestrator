"""The normal terminal merge path consumes opted-in exact-head review evidence."""

from __future__ import annotations

import subprocess

import pytest

import merge_guard


def guarded(checkpoint, merges, panels):
    return merge_guard.guarded_merge(
        "o/r#9",
        expected_head="a" * 40,
        confirm_merge=True,
        metadata_fn=lambda target: {
            "target": target,
            "title": "bounded work",
            "labels": [],
            "state": "OPEN",
            "is_draft": False,
            "closing_issues": ["o/r#1"],
            "head_branch": "codex/issue-1-work",
        },
        gate_fn=lambda *args, **kwargs: None,
        preflight_fn=lambda target, expected_head: {"blocked": False, "head": expected_head},
        panel_fn=lambda *args, **kwargs: panels.append(True) or {"status": "disabled"},
        adjudicate_fn=lambda *args, **kwargs: None,
        review_loop_fn=checkpoint,
        merge_fn=lambda cmd, **kwargs: merges.append(cmd)
        or subprocess.CompletedProcess(cmd, 0, "merged", ""),
        remote_runs_fn=lambda *args, **kwargs: [],
        spawn_fn=lambda *args, **kwargs: {"status": "disabled"},
    )


def test_pending_loop_blocks_the_normal_guard_before_merge_or_panel():
    merges, panels, reads = [], [], []

    def checkpoint(target, head, **context):
        reads.append((target, head, context))
        return {"required": True, "eligible": False, "reason": "review-loop-pending"}

    result = guarded(checkpoint, merges, panels)
    assert result["blocked"] and not result["merge_executed"]
    assert not merges and not panels
    assert reads[0][2] == {"source_targets": ["o/r#1"], "head_branch": "codex/issue-1-work"}


def test_loop_is_rechecked_immediately_before_terminal_merge():
    results = iter(
        [
            {"required": True, "eligible": True},
            {"required": True, "eligible": False, "reason": "new pending round"},
        ]
    )
    merges, panels = [], []
    result = guarded(lambda *args, **kwargs: next(results), merges, panels)
    assert result["blocked"] and result["reason"] == "new pending round"
    assert not merges and panels == [True]


@pytest.mark.parametrize("required", [False, True])
def test_unopted_or_exact_head_approved_loop_keeps_all_normal_merge_gates(required):
    merges, panels = [], []
    result = guarded(
        lambda *args, **kwargs: {"required": required, "eligible": True}, merges, panels
    )
    assert result["merge_executed"] and not result["blocked"]
    assert len(merges) == 1 and panels == [True]
    assert "--match-head-commit" in merges[0] and "a" * 40 in merges[0]
