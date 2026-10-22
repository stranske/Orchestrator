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
ON: dict[str, str] = {}  # on by default since 2026-10-12: one router-chosen reviewer
OFF = {"ORCH_RUN_ADVERSARIAL_REVIEW": "0"}


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


def _merge(
    panel=None,
    *,
    labels=("risk:major",),
    env=ON,
    head=HEAD,
    merges=None,
    spawned=None,
    defer_advisory_until_verification=False,
):
    merges = [] if merges is None else merges
    spawned = [] if spawned is None else spawned

    def fake_merge(cmd, capture_output=True, text=True):
        merges.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="merged", stderr="")

    def panel_fn(target, metadata, *, head, env):
        return merge_guard.adversarial_review_status(
            target, metadata, head=head, env=env, run_fn=_issue_labels(*labels)
        )

    def spawn(target, head, context, exclude):
        spawned.append({"target": target, "head": head, "context": context, "exclude": exclude})
        return {"status": "scheduled_after_merge", "pid": None}

    return merge_guard.guarded_merge(
        "o/r#41",
        expected_head=head,
        confirm_merge=head is not None,
        defer_advisory_until_verification=defer_advisory_until_verification,
        env=env,
        metadata_fn=_meta,
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=_clean,
        merge_fn=fake_merge,
        remote_runs_fn=lambda target, mode=None: [],
        panel_fn=panel_fn,
        spawn_fn=spawn,
    )


def _judge(panel, *, head=HEAD, labels=("risk:major",)):
    """What the detached post-merge job does: judge the merged head now and record it."""
    return merge_guard.adversarial_review_status(
        "o/r#41",
        _meta("o/r#41"),
        head=head,
        env=ON,
        run_fn=_issue_labels(*labels),
        run_now=True,
        worktree="/wt/at-head",
        head_fn=lambda worktree: head,
        choose_fn=lambda exclude: {"agent": "cursor", "reason": "stub"},
        review_fn=panel,
    )


def test_a_source_issue_risk_label_makes_the_merge_high_stakes_and_the_panel_advisory(brain):
    """The risk label lives on the issue (no fleet PR carries `risk:*`). The merge is never held:
    the panel is scheduled AFTER it lands, detached, with the PR's author agent excluded, and its
    verdict, judged on that head, is BLOCKED for the follow-up to act on (owner, 2026-10-12)."""
    panel, merges, spawned = Panel("BLOCKED"), [], []
    out = _merge(merges=merges, spawned=spawned)
    review = out["adversarial_review"]
    assert review["status"] == "deferred_to_post_merge", review
    assert review["reason"] == "high-stakes label: risk:major", review
    assert out["blocked"] is False and out["merge_executed"] is True and merges, out
    assert review["post_merge"]["status"] == "scheduled_after_merge", review
    assert spawned[0]["exclude"] == {"codex"} and "risk:major" in spawned[0]["context"], spawned
    judged = _judge(panel)
    assert (judged["status"], judged["verdict"]) == ("executed", "BLOCKED"), judged
    assert judged["reviewed_by"] == ["cursor"] and "risk:major" in panel.contexts[0], judged


def test_a_dry_run_schedules_nothing(brain):
    spawned: list = []
    out = merge_guard.guarded_merge(
        "o/r#41",
        expected_head=HEAD,
        env=ON,
        metadata_fn=_meta,
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=_clean,
        panel_fn=lambda t, m, *, head, env: merge_guard.adversarial_review_status(
            t, m, head=head, env=env, run_fn=_issue_labels("risk:major")
        ),
        spawn_fn=lambda *a: spawned.append(a),
    )
    assert out["adversarial_review"]["status"] == "deferred_to_post_merge" and spawned == [], out


def test_an_injected_merge_never_spawns_a_real_panel(brain, monkeypatch):
    import adversarial

    monkeypatch.setattr(
        adversarial, "spawn_post_merge_panel", lambda *a, **k: pytest.fail("spawned a real panel")
    )
    out = merge_guard.guarded_merge(
        "o/r#41",
        expected_head=HEAD,
        confirm_merge=True,
        env=ON,
        metadata_fn=_meta,
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=_clean,
        merge_fn=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "merged", ""),
        remote_runs_fn=lambda target, mode=None: [],
        panel_fn=lambda t, m, *, head, env: merge_guard.adversarial_review_status(
            t, m, head=head, env=env, run_fn=_issue_labels("risk:major")
        ),
    )
    assert out["adversarial_review"]["post_merge"]["status"] == "not_scheduled", out


def test_a_second_merge_attempt_at_the_same_head_reuses_the_verdict(brain):
    panel = Panel("BLOCKED")
    _judge(panel)
    again = _merge()
    assert again["adversarial_review"]["status"] == "reused", again
    assert len(panel.contexts) == 1, panel.contexts


def test_with_the_flag_off_a_recorded_verdict_is_still_reported_and_nothing_runs(brain):
    panel = Panel("PASS")
    _judge(panel)
    quiet = _merge(env=OFF)
    assert quiet["adversarial_review"]["status"] == "reused", quiet
    unjudged = _merge(env=OFF, head="b" * 40)["adversarial_review"]
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


def test_cached_premerge_pass_still_schedules_merge_commit_review(brain):
    _judge(Panel("PASS"))
    spawned = []
    out = _merge(spawned=spawned)
    assert out["merge_executed"] is True
    assert out["adversarial_review"]["post_merge"]["status"] == "scheduled_after_merge"
    assert len(spawned) == 1 and spawned[0]["exclude"] == {"codex"}


def test_disabled_panel_never_schedules_cached_blocker(brain):
    _judge(Panel("BLOCKED"))
    spawned = []
    out = _merge(env=OFF, spawned=spawned)
    assert out["merge_executed"] is True
    assert spawned == []
    assert "post_merge" not in out["adversarial_review"]


def test_closer_defers_high_stakes_panel_and_adjudication_until_verifier(brain, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("deferred advisory work was invoked")

    monkeypatch.setattr(merge_guard, "adversarial_review_status", forbidden)
    monkeypatch.setattr(merge_guard, "adjudicate_disagreement", forbidden)
    merges, spawned = [], []
    out = _merge(merges=merges, spawned=spawned, defer_advisory_until_verification=True)
    assert out["merge_executed"] is True and out["blocked"] is False
    assert len(merges) == 1 and spawned == []
    panel = out["adversarial_review"]
    assert panel["status"] == "deferred_until_verification"
    assert panel["acceptance_authority"] is False and "verdict" not in panel
    assert "verifier evaluation" in panel["detail"]
    assert "adjudication" not in out
    assert "--match-head-commit" in merges[0] and HEAD in merges[0]


@pytest.mark.parametrize(
    "boundary", ["missing_head", "final_preflight", "runtime_gate", "review_loop"]
)
def test_advisory_deferral_preserves_objective_merge_refusals(brain, boundary):
    calls = []

    def preflight(target, *, expected_head):
        calls.append(expected_head)
        if boundary == "final_preflight" and len(calls) == 2:
            return {"blocked": True, "reason": "head changed"}
        return _clean(target, expected_head=expected_head)

    out = merge_guard.guarded_merge(
        "o/r#41",
        expected_head=None if boundary == "missing_head" else HEAD,
        confirm_merge=True,
        defer_advisory_until_verification=True,
        metadata_fn=_meta,
        gate_fn=lambda *args, **kwargs: (
            {"status": "executed", "verdict": "FAIL", "blocks": True}
            if boundary == "runtime_gate"
            else None
        ),
        preflight_fn=preflight,
        review_loop_fn=lambda *args, **kwargs: {
            "required": boundary == "review_loop",
            "eligible": False,
            "reason": "review floor pending",
        },
        merge_fn=lambda *args, **kwargs: pytest.fail("merge bypassed objective refusal"),
        spawn_fn=lambda *args, **kwargs: pytest.fail("spawned deferred advisory"),
    )
    assert out["blocked"] is True and out["merge_executed"] is False


def test_advisory_deferral_cli_is_explicit_and_defaults_off(monkeypatch, capsys):
    received = []
    monkeypatch.setattr(
        merge_guard,
        "guarded_merge",
        lambda *args, **kwargs: received.append(kwargs) or {"blocked": False},
    )
    assert (
        merge_guard.main(["o/r#41", "--expected-head", HEAD, "--defer-advisory-until-verification"])
        == 0
    )
    assert merge_guard.main(["o/r#41", "--expected-head", HEAD]) == 0
    assert [row["defer_advisory_until_verification"] for row in received] == [True, False]


@pytest.mark.parametrize(
    "required_by", ["label", "forced", "spec", "broken_spec", "broken_symlink"]
)
def test_remote_only_refuses_local_runtime_footprints_before_any_execution(brain, required_by):
    import runtime_ac_gate

    path = runtime_ac_gate.spec_path("o/r#41")
    path.parent.mkdir(parents=True, exist_ok=True)
    if required_by in {"spec", "broken_spec"}:
        path.write_text("{}" if required_by == "spec" else "not JSON")
    if required_by == "broken_symlink":
        path.symlink_to(brain / "missing-spec")
    meta = _meta("o/r#41")
    if required_by == "label":
        meta["labels"].append("runtime-ac")
    out = merge_guard.guarded_merge(
        "o/r#41",
        expected_head=HEAD,
        confirm_merge=True,
        remote_only=True,
        require_runtime_ac=required_by == "forced",
        metadata_fn=lambda target: meta,
        preflight_fn=_clean,
        gate_fn=lambda *args, **kwargs: pytest.fail("remote-only ran runtime commands"),
        merge_fn=lambda *args, **kwargs: pytest.fail("remote-only bypassed required gate"),
        panel_fn=lambda *args, **kwargs: pytest.fail("remote-only invoked advisory"),
    )
    assert out["blocked"] is True and out["merge_executed"] is False
    assert out["reason"] == "footprint_requires_local_runtime_ac"


def test_remote_only_keeps_exact_head_guard_and_never_runs_local_advisory(brain):
    merges, metadata_calls = [], []

    def metadata(target):
        metadata_calls.append(target)
        return _meta(target, title="security: high stakes")

    out = merge_guard.guarded_merge(
        "o/r#41",
        expected_head=HEAD,
        confirm_merge=True,
        remote_only=True,
        metadata_fn=metadata,
        preflight_fn=_clean,
        gate_fn=lambda *args, **kwargs: pytest.fail("remote-only called command-capable gate"),
        review_loop_fn=lambda *args, **kwargs: {"required": False},
        panel_fn=lambda *args, **kwargs: pytest.fail("remote-only invoked panel"),
        adjudicate_fn=lambda *args, **kwargs: pytest.fail("remote-only invoked adjudicator"),
        spawn_fn=lambda *args, **kwargs: pytest.fail("remote-only spawned advisory"),
        merge_fn=lambda command, **kwargs: merges.append(command)
        or subprocess.CompletedProcess(command, 0, "merged", ""),
        remote_runs_fn=lambda *args, **kwargs: [],
    )
    assert out["blocked"] is False and out["merge_executed"] is True
    assert len(metadata_calls) == 2
    assert out["remote_only_guard_final"]["remote_only"] is True
    assert "--match-head-commit" in merges[0] and HEAD in merges[0]
    assert out["adversarial_review"]["status"] == "deferred_until_verification"


@pytest.mark.parametrize(
    "change", ["required_spec", "broken_spec", "shadow_spec", "required_label"]
)
def test_remote_only_rechecks_live_metadata_and_spec_immediately_before_merge(brain, change):
    import runtime_ac_gate

    calls = []

    def metadata(target):
        calls.append(target)
        meta = _meta(target)
        if len(calls) == 2:
            if change == "required_label":
                meta["labels"].append("runtime-ac")
            else:
                path = runtime_ac_gate.spec_path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    {
                        "required_spec": "{}",
                        "broken_spec": "broken",
                        "shadow_spec": '{"verification":{"shadow_only":true}}',
                    }[change]
                )
        return meta

    out = merge_guard.guarded_merge(
        "o/r#41",
        expected_head=HEAD,
        confirm_merge=True,
        remote_only=True,
        metadata_fn=metadata,
        preflight_fn=_clean,
        review_loop_fn=lambda *args, **kwargs: {"required": False},
        gate_fn=lambda *args, **kwargs: pytest.fail("runtime command executed"),
        merge_fn=lambda *args, **kwargs: pytest.fail("merge ignored footprint drift"),
    )
    assert out["blocked"] is True and out["merge_executed"] is False
    assert out["reason"] == "footprint_requires_local_runtime_ac"


def test_remote_only_cli_is_explicit(monkeypatch):
    received = []
    monkeypatch.setattr(
        merge_guard,
        "guarded_merge",
        lambda *args, **kwargs: received.append(kwargs) or {"blocked": False},
    )
    merge_guard.main(["o/r#41", "--remote-only"])
    merge_guard.main(["o/r#41"])
    assert [row["remote_only"] for row in received] == [True, False]
