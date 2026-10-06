"""The adversarial panel judges each (PR, head SHA) once, and a moved head is never shown an old
verdict.

THE DEFECT (measured 2026-10-05). The panel's only automatic caller was the tick's pre-delegation
hook, which ran on every hourly tick for every high-stakes closer item and keyed nothing: the same
head would have been reviewed again each hour, and the Brain event it recorded did not say which
head it had judged. `adversarial.review_at_head` is now the one entry, keyed on a hash over
(target, head, reviewer set) and recorded in the Brain's completion events.

These tests drive the real recorder and lookup against a disposable Brain; only the reviewers, the
worktree and its head are stubbed.
"""

from __future__ import annotations

import json

import pytest

import adversarial
import feedback

TARGET = "o/r#7"
REVIEWERS = ["vibe", "gemini"]


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


class Panel:
    """Stub reviewers: counts every real judgment and returns the verdict it is set to."""

    def __init__(self, verdict: str = "BLOCKED") -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, worktree, reviewers, context):
        self.calls.append(worktree)
        blockers = [{"severity": "high", "finding": "policy fails open", "confidence": 0.9}]
        return {"verdict": self.verdict, "blockers": blockers if self.verdict == "BLOCKED" else []}


def _judge(panel: Panel, head: str, **kwargs):
    return adversarial.review_at_head(
        TARGET,
        head,
        reviewers=REVIEWERS,
        worktree=f"/wt/{head[:6]}",
        head_fn=lambda worktree: head,
        review_fn=panel,
        **kwargs,
    )


def test_the_same_head_is_judged_once_and_its_findings_come_back(brain):
    panel = Panel("BLOCKED")
    first = _judge(panel, "a" * 40)
    assert (first["status"], first["memo"], first["verdict"]) == ("executed", "none", "BLOCKED")
    assert first["inconclusive_at_head"] == 0, first  # a count, so zero is printed as zero
    again = _judge(panel, "a" * 40)
    assert (again["status"], again["memo"], again["verdict"]) == ("reused", "found", "BLOCKED")
    assert again["result"]["blockers"][0]["finding"] == "policy fails open", again
    assert len(panel.calls) == 1, panel.calls


def test_a_moved_head_is_judged_afresh_and_never_shown_the_old_verdict(brain):
    panel = Panel("BLOCKED")
    _judge(panel, "a" * 40)
    panel.verdict = "PASS"
    moved = _judge(panel, "b" * 40)
    assert (moved["status"], moved["verdict"]) == ("executed", "PASS"), moved
    assert len(panel.calls) == 2, panel.calls
    assert adversarial.recorded_verdict(TARGET, "a" * 40, REVIEWERS)["verdict"] == "BLOCKED"
    assert adversarial.recorded_verdict(TARGET, "b" * 40, REVIEWERS)["verdict"] == "PASS"


def test_another_reviewer_set_is_another_judgment(brain):
    panel = Panel("PASS")
    _judge(panel, "a" * 40)
    assert adversarial.recorded_verdict(TARGET, "a" * 40, ["codex"])["state"] == "none"


def test_an_inconclusive_panel_is_re_run_at_the_same_head(brain):
    """A reviewer shortfall is "NOT a pass; re-run the missing coverage". Reusing it would forbid
    exactly that re-run until an unrelated push, so only PASS and BLOCKED are reused."""
    panel = Panel("INCONCLUSIVE")
    assert _judge(panel, "c" * 40)["conclusive"] is False
    panel.verdict = "PASS"
    rerun = _judge(panel, "c" * 40)
    assert (rerun["status"], rerun["verdict"]) == ("executed", "PASS"), rerun
    assert rerun["inconclusive_at_head"] == 1 and len(panel.calls) == 2, rerun
    assert _judge(panel, "c" * 40)["status"] == "reused"


def test_a_worktree_at_another_commit_is_refused_and_nothing_is_recorded(brain):
    panel = Panel("PASS")
    out = adversarial.review_at_head(
        TARGET,
        "d" * 40,
        reviewers=REVIEWERS,
        worktree="/wt/stale",
        head_fn=lambda worktree: "e" * 40,
        review_fn=panel,
    )
    assert out["status"] == "head_mismatch" and out["observed_head"] == "e" * 40, out
    assert panel.calls == [], panel.calls
    assert adversarial.recorded_verdict(TARGET, "d" * 40, REVIEWERS)["state"] == "none"


def test_an_unreadable_brain_is_unknown_not_none_and_the_panel_still_runs(brain):
    panel = Panel("PASS")
    out = _judge(panel, "f" * 40, lookup_fn=lambda *a: {"state": "unknown", "error": "locked"})
    assert (out["status"], out["memo"]) == ("executed", "unknown"), out
    assert out["inconclusive_at_head"] is None and out["memo_error"] == "locked", out


def test_findings_are_served_only_while_the_artifact_matches_its_recorded_hash(brain):
    panel = Panel("BLOCKED")
    _judge(panel, "a" * 40)
    path = adversarial._panel_artifact_path(adversarial.panel_key(TARGET, "a" * 40, REVIEWERS))
    doc = json.loads(path.read_text())
    doc["result"]["blockers"] = []
    path.write_text(json.dumps(doc))
    reused = _judge(panel, "a" * 40)
    assert reused["status"] == "reused" and reused["verdict"] == "BLOCKED", reused
    assert reused["result"] is None, reused  # the verdict stands; edited findings are not served


@pytest.mark.parametrize("target,head", [("o/r", "a" * 40), (TARGET, "abc123"), (TARGET, "")])
def test_a_target_or_head_that_cannot_key_a_verdict_is_refused(brain, target, head):
    assert adversarial.review_at_head(target, head, reviewers=REVIEWERS)["status"] == "invalid"


def test_the_cli_reports_a_recorded_verdict_without_running_the_panel(brain, capsys):
    _judge(Panel("BLOCKED"), "a" * 40)
    argv = ["review", "--target", TARGET, "--head", "a" * 40, "--reviewers", "vibe,gemini"]
    assert adversarial.main(argv + ["--lookup-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["state"], out["verdict"]) == ("found", "BLOCKED"), out
    assert adversarial.main(["review", "--target", TARGET, "--head", "zz", "--lookup-only"]) == 2
