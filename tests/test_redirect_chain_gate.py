"""Stage-2 credit and the dry-run path out of its gate, with private state."""

import json
from pathlib import Path

import pytest

import capabilities
import feedback
import keepalive_supervisor
import redirect_apply
import redirect_shadow
import roles


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(roles.router, "load_capacity", lambda: {})
    monkeypatch.setattr(roles.router, "select_agent", lambda *a, **kw: {"agent": "codex"})


@pytest.mark.parametrize("action,agent", [("decompose", "cursor"), ("redirect", "codex")])
def test_any_differing_applied_action_earns_disagreement_credit(tmp_path, action, agent):
    corpus = tmp_path / "corpus.jsonl"
    row = redirect_shadow.build_entry(
        {
            "proposal": {"action": action, "switch_agent": agent},
            "baseline": {"action": "redirect"},
            "decision_source": "redirect_agent",
            "role_run_id": "role-1",
            "plan": {
                "action": action,
                "steps": [{"id": "delegate-retry", "commands": [["dispatch", "--agent", agent]]}],
            },
        },
        {"target": "o/r#1", "agent": "cursor"},
        "gate",
        source="live-dispatch",
    )
    redirect_shadow._append_event(row, corpus)
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == 0
    link = {
        "kind": "redirect_outcome_link",
        "role_run_id": "role-1",
        "accepted": True,
        "link_result": {"synced": True},
    }
    redirect_shadow._append_event(link, corpus)
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == 1
    assert row["disagreement"] == (action != "redirect")  # historical raw field stays raw

    def record_application(applied_action, *, applied=True):
        redirect_shadow.record_apply(
            role_run_id="role-1",
            target="o/r#1",
            plan_action=applied_action,
            authorization={"allowed": True},
            apply_result={"applied": applied},
            dry_run=not applied,
            corpus_path=corpus,
        )

    # An applied action supersedes the proposal; an unapplied preview cannot replace it.
    record_application("inspect")
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == 0
    record_application("decompose", applied=False)
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == 0
    record_application("decompose")
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == 1
    record_application("redirect")
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == int(agent != "cursor")

    # Redirect also earns action credit against a decompose baseline with the same worker.
    reverse_corpus = tmp_path / "reverse.jsonl"
    reverse_row = {
        **row,
        "baseline_action": "decompose",
        "baseline_agent": agent,
        "baseline": {"action": "decompose"},
        "disagreement": action != "decompose",
    }
    redirect_shadow._append_event(reverse_row, reverse_corpus)
    redirect_shadow._append_event(link, reverse_corpus)
    redirect_shadow.record_apply(
        role_run_id="role-1",
        target="o/r#1",
        plan_action="redirect",
        authorization={"allowed": True},
        apply_result={"applied": True},
        dry_run=False,
        corpus_path=reverse_corpus,
    )
    assert redirect_shadow.summarize(reverse_corpus)["linked_disagreements"] == 1
    assert redirect_shadow._iter_events(corpus)[0] == row
    assert redirect_shadow._iter_events(reverse_corpus)[0] == reverse_row
    redirect_shadow._append_event({**link, "accepted": False}, corpus)
    assert redirect_shadow.summarize(corpus)["linked_disagreements"] == 0


def test_link_step_survives_one_bad_row_and_counts_it(tmp_path, monkeypatch, capsys):
    rows = [{"role_run_id": r, "influenced_run_id": "work-" + r} for r in ["bad", "good"]]
    monkeypatch.setattr(redirect_apply, "pending_outcome_links", lambda **kw: rows)
    calls = []

    def link(role, run, **kw):
        calls.append(role)
        if role == "bad":
            raise ValueError("unrecorded role run")
        return {"event": {"link_result": {"synced": True}}}

    monkeypatch.setattr(redirect_shadow, "link_outcome", link)
    corpus = tmp_path / "corpus.jsonl"
    result = redirect_apply.link_applied_outcomes(corpus_path=corpus)
    assert calls == ["bad", "good"]
    assert result["linked"] == result["failed"] == 1
    assert result["failures"][0]["reason"] == "unrecorded role run"
    assert redirect_shadow._iter_events(corpus)[0]["kind"] == "redirect_outcome_link_failed"
    monkeypatch.setattr(redirect_apply, "link_applied_outcomes", lambda **kw: result)
    assert redirect_apply.main(["--link-outcomes"]) == 0
    assert "linked 1, failed 1" in capsys.readouterr().out


@pytest.mark.parametrize("action,agent", [("redirect", "cursor"), ("inspect", "codex")])
def test_same_or_unapplied_decision_gets_no_credit(action, agent):
    row = {
        "baseline_action": "redirect",
        "baseline_agent": "cursor",
        "proposal_action": "decompose",
        "disagreement": True,
        "plan": {"action": action, "next_agent": agent},
    }
    assert not redirect_shadow.applied_disagreement(row)


def test_dry_link_preview_does_not_write_failures_or_join(tmp_path, monkeypatch):
    corpus = tmp_path / "absent.jsonl"
    monkeypatch.setattr(
        redirect_apply,
        "pending_outcome_links",
        lambda **kw: [{"role_run_id": "bad", "influenced_run_id": "work"}],
    )

    def forbidden(*a, **kw):
        raise AssertionError("preview must not join")

    monkeypatch.setattr(redirect_shadow, "link_outcome", forbidden)
    result = redirect_apply.link_applied_outcomes(dry_run=True, corpus_path=corpus)
    assert result["pending"] == 1 and result["linked"] == result["failed"] == 0
    assert not corpus.exists()


def test_an_open_gate_writes_a_plan_and_heartbeats(tmp_path, monkeypatch):
    report = json.loads((Path(__file__).parent / "fixtures/redirect_stalls/auth.json").read_text())
    monkeypatch.setattr(keepalive_supervisor, "live_targets", lambda **kw: [report["target"]])
    monkeypatch.setattr(
        keepalive_supervisor,
        "plan_target",
        lambda *a, **kw: {
            "target": report["target"],
            "eligible": True,
            "stage2_record_command": ["record"],
            "report": report,
        },
    )
    monkeypatch.setattr(
        keepalive_supervisor, "_recorded_proposal_targets", lambda *a, **kw: {report["target"]}
    )
    monkeypatch.setattr(
        redirect_shadow, "summarize", lambda *a: {"ready_for_supervised_apply": True}
    )
    monkeypatch.setattr(
        redirect_shadow, "collect_historical_from_keepalive", lambda **kw: {"would_collect": 0}
    )
    events = []
    monkeypatch.setattr(keepalive_supervisor, "_capability_heartbeat", events.append)
    result = keepalive_supervisor.stage2_acquisition_plan(report_dir=tmp_path)
    artifact = Path(result["supervised_apply_plan"])
    plan = json.loads(artifact.read_text())
    assert plan["target"] == report["target"]
    assert plan["dry_run_only"] is True and result["live_action_enabled"] is False
    assert "codex" in plan["steps"][-1]["commands"][0]
    assert events == ["success"]
    assert result["status"] == "ready_for_supervised_apply_review"


def test_replay_runs_the_fixtures_to_authorize_with_named_agents(capsys, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("dry replay must not dispatch or learn")

    monkeypatch.setattr(roles.dispatcher, "offload", forbidden)
    monkeypatch.setattr(feedback, "record_role_run", forbidden)
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    heartbeats = []
    monkeypatch.setattr(capabilities, "heartbeat", lambda *a, **kw: heartbeats.append(a))
    fixtures = Path(__file__).parent / "fixtures/redirect_stalls"
    result = redirect_apply.replay_stalls(fixtures)
    assert result["authorized"] == result["total"] == 3
    assert {row["fixture"] for row in result["results"]} == {
        "auth.json",
        "exited.json",
        "drift.json",
    }
    assert all(row["agent"] and row["authorization"]["allowed"] for row in result["results"])
    assert all(not row["authorization"]["would_mutate"] for row in result["results"])
    assert redirect_apply.main(["--replay-stalls", str(fixtures)]) == 0
    assert "authorized 3/3 with named agents" in capsys.readouterr().out
    assert not heartbeats
