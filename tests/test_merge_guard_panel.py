"""The closer-PR review hooks run at the terminal merge, the decision a closer PR actually faces.

Until 2026-10-05 the tick ran the runtime-AC gate and the adversarial panel before a remote
delegation that no closer item can reach (discovery lists a closer only when its PR already carries
an `agent:*` label, and the dispatcher refuses every such label), and nothing read what they said.
`merge_guard` already ran the runtime-AC gate; it now also gives a high-stakes PR the panel, judged
once per exact head and advisory as the owner set it, and sends a conclusive disagreement between
the two verdicts to the adjudicator role.

Also pinned here: `merge_guard`'s own selftest no longer writes to the Brain it is pointed at. From
2026-08-21 it wrote five runtime-AC gate events per run for its fixtures `o/r#5` and `o/r#6`, 3,702
in all, and the live runtime-AC flow monitor reported them as live firing.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

import feedback
import merge_guard

HEAD = "a" * 40
ON = {"ORCH_RUN_ADVERSARIAL_REVIEW": "1", "ORCH_ADVERSARIAL_REVIEWERS": "vibe,gemini"}
OFF = {"ORCH_ADVERSARIAL_REVIEWERS": "vibe,gemini"}


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


def _issue_labels(*names):
    doc = {"labels": [{"name": name} for name in names]}
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(doc), "")


def _meta(target, title="routine copy edit"):
    return {
        "target": target,
        "labels": ["agent:codex"],
        "title": title,
        "state": "OPEN",
        "is_draft": False,
        "closing_issues": ["o/r#40"],
    }


def _clean(target, *, expected_head):
    return {"target": target, "head": expected_head, "blocked": False, "reason": None}


class Panel:
    def __init__(self, verdict="BLOCKED"):
        self.verdict = verdict
        self.contexts: list[str] = []

    def __call__(self, worktree, reviewers, context):
        self.contexts.append(context)
        return {"verdict": self.verdict, "blockers": [{"severity": "high", "finding": "x"}]}


def _merge(panel, *, labels=("risk:major",), env=ON, head=HEAD, merges=None):
    merges = [] if merges is None else merges

    def fake_merge(cmd, capture_output=True, text=True):
        merges.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")

    def panel_fn(target, metadata, *, head, env):
        return merge_guard.adversarial_review_status(
            target,
            metadata,
            head=head,
            env=env,
            run_fn=_issue_labels(*labels),
            worktree="/wt/at-head",
            head_fn=lambda worktree: head,
            review_fn=panel,
        )

    return merge_guard.guarded_merge(
        "o/r#41",
        expected_head=head,
        confirm_merge=head is not None,
        env=env,
        metadata_fn=_meta,
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=_clean,
        merge_fn=fake_merge,
        remote_runs_fn=lambda target, mode=None: [],
        panel_fn=panel_fn,
    )


def test_a_source_issue_risk_label_makes_the_merge_high_stakes_and_the_panel_advisory(brain):
    """The risk label lives on the issue (no fleet PR carries `risk:*`). BLOCKED is reported beside
    the merge for the merger to verify, and the merge still runs: the owner set the panel advisory.
    """
    panel, merges = Panel("BLOCKED"), []
    out = _merge(panel, merges=merges)
    review = out["adversarial_review"]
    assert (review["status"], review["verdict"]) == ("executed", "BLOCKED"), review
    assert review["reason"] == "high-stakes label: risk:major", review
    assert out["blocked"] is False and out["merge_executed"] is True and merges, out
    assert "risk:major" in panel.contexts[0], panel.contexts


def test_a_second_merge_attempt_at_the_same_head_reuses_the_verdict(brain):
    panel = Panel("BLOCKED")
    _merge(panel)
    again = _merge(panel)
    assert again["adversarial_review"]["status"] == "reused", again
    assert len(panel.contexts) == 1, panel.contexts


def test_with_the_flag_off_a_recorded_verdict_is_still_reported_and_nothing_runs(brain):
    panel = Panel("PASS")
    _merge(panel)
    quiet = _merge(panel, env=OFF)
    assert quiet["adversarial_review"]["status"] == "reused", quiet
    unjudged = _merge(panel, env=OFF, head="b" * 40)["adversarial_review"]
    assert (unjudged["status"], unjudged["memo"]) == ("required_but_not_run", "none"), unjudged
    assert "adversarial.py review --target o/r#41 --head " + "b" * 40 in unjudged["detail"]
    assert len(panel.contexts) == 1, panel.contexts


def test_routine_work_is_routine_and_unread_labels_are_unknown(brain):
    assert _merge(Panel(), labels=("bug",))["adversarial_review"] == {"status": "routine"}
    unread = merge_guard.adversarial_review_status(
        "o/r#41",
        _meta("o/r#41"),
        head=HEAD,
        env=ON,
        run_fn=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "HTTP 502"),
    )
    assert unread["status"] == "unknown" and "HTTP 502" in unread["detail"], unread


def test_a_high_stakes_title_needs_no_label_read(brain):
    out = merge_guard.adversarial_review_status(
        "o/r#41",
        {**_meta("o/r#41", title="security: rotate the signing key"), "closing_issues": []},
        head=None,
        env=ON,
    )
    assert out["status"] == "no_head" and "--expected-head" in out["detail"], out


def test_a_panel_that_raises_never_blocks_the_merge(brain):
    def boom(*args, **kwargs):
        raise RuntimeError("provision failed")

    out = merge_guard.guarded_merge(
        "o/r#43",
        expected_head=HEAD,
        confirm_merge=True,
        metadata_fn=_meta,
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=_clean,
        merge_fn=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "merged", ""),
        remote_runs_fn=lambda target, mode=None: [],
        panel_fn=boom,
    )
    assert out["adversarial_review"] == {"status": "error", "error": "provision failed"}, out
    assert out["merge_executed"] is True and out["blocked"] is False, out


def test_no_panel_is_paid_for_a_pr_the_deterministic_gate_blocks(brain):
    called: list = []
    out = merge_guard.guarded_merge(
        "o/r#44",
        expected_head=HEAD,
        confirm_merge=True,
        metadata_fn=_meta,
        gate_fn=lambda item, **kwargs: {"status": "executed", "verdict": "FAIL", "blocks": True},
        preflight_fn=_clean,
        merge_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("merged")),
        panel_fn=lambda *a, **k: called.append(a),
    )
    assert out["blocked"] is True and "adversarial_review" not in out and called == [], out


@pytest.mark.parametrize(
    "gate,panel,adjudicated",
    [
        ({"status": "executed", "verdict": "PASS"}, {"verdict": "BLOCKED"}, True),
        ({"status": "executed", "verdict": "PASS"}, {"verdict": "PASS"}, False),
        ({"status": "executed", "verdict": "PASS"}, {"verdict": "INCONCLUSIVE"}, False),
        (None, {"verdict": "BLOCKED"}, False),
        ({"status": "executed", "verdict": "PASS"}, {"status": "routine"}, False),
    ],
)
def test_only_a_conclusive_disagreement_reaches_the_adjudicator(brain, gate, panel, adjudicated):
    calls: list = []

    def activate(item, gate, review, cap, **kwargs):
        calls.append((gate.get("verdict"), review["result"]["verdict"]))
        return {"selector": {"selector_status": "matched_not_invoked"}, "result": None}

    out = merge_guard.adjudicate_disagreement(
        "o/r#41", gate, panel, env={}, dry_run=False, activate_fn=activate
    )
    assert (out is not None) is adjudicated and len(calls) == int(adjudicated), (out, calls)


def test_pr_metadata_names_the_issues_the_pr_closes():
    doc = {
        "title": "T",
        "state": "OPEN",
        "isDraft": False,
        "labels": [],
        "closingIssuesReferences": [
            {"number": 4, "repository": {"name": "other", "owner": {"login": "o"}}},
            {"number": 9},
        ],
    }
    meta = merge_guard.pr_metadata(
        "o/r#5", run_fn=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(doc), "")
    )
    assert meta["closing_issues"] == ["o/other#4", "o/r#9"], meta


def test_the_selftest_writes_nothing_to_the_brain_it_is_pointed_at(tmp_path, monkeypatch):
    """Point the module at a Brain that stands in for the live one; the selftest must leave it
    exactly as it found it, while its own disposable Brain still receives the fixture events."""
    live = tmp_path / "live.db"
    monkeypatch.setattr(feedback, "DB_PATH", live)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    feedback.record_runtime_ac_gate_event(
        target="o/r#1", gate_status="skipped", required=False, dry_run=True
    )
    merge_guard._selftest()
    with sqlite3.connect(live) as c:
        rows = c.execute(
            "SELECT COUNT(*) FROM completion_events WHERE producer='runtime_ac_gate'"
        ).fetchone()[0]
    assert rows == 1, f"merge_guard's selftest wrote {rows - 1} rows into the Brain it was given"
    assert feedback.DB_PATH == live
