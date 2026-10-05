"""Automatic redirect must never release a replacement lane's claim."""

import subprocess

import pytest

import redirect_plan


@pytest.mark.parametrize(
    "replacement",
    [
        {"target": "o/r#1", "agent": "codex", "pid": 20, "ts": 2},
        {"target": "o/r#1", "agent": "codex", "pid": 10, "ts": 2},
        {"target": "o/r#1", "agent": "other", "pid": 10, "ts": 1},
    ],
)
def test_replacement_claim_refuses_before_any_mutation(tmp_path, monkeypatch, replacement):
    old = {"target": "o/r#1", "agent": "codex", "pid": 10, "ts": 1}
    monkeypatch.setattr(redirect_plan.claims, "holder", lambda _: replacement)
    monkeypatch.setattr(redirect_plan.capabilities, "production_heartbeat", lambda *a, **k: None)
    calls = []
    prompt = tmp_path / "prompt.md"
    plan = {
        "action": "redirect",
        "target": "o/r#1",
        "prompt_text": "retry",
        "prompt_file": str(prompt),
        "lane_guard": {"claim_snapshot": old, "pid": 10},
        "steps": [{"id": "release-claim", "commands": [["release", "o/r#1"]]}],
    }
    with pytest.raises(ValueError, match="identity changed"):
        redirect_plan.apply_plan(
            plan,
            confirm_target="o/r#1",
            runner=lambda *a, **k: calls.append(a),
            pid_checker=lambda _: False,
        )
    assert calls == []
    assert not prompt.exists()


def test_same_claim_live_pid_is_refused(tmp_path, monkeypatch):
    current = {"target": "o/r#1", "agent": "codex", "pid": 10, "ts": 1}
    monkeypatch.setattr(redirect_plan.claims, "holder", lambda _: current)
    monkeypatch.setattr(redirect_plan.capabilities, "production_heartbeat", lambda *a, **k: None)
    plan = {
        "action": "redirect",
        "target": "o/r#1",
        "prompt_text": "retry",
        "prompt_file": str(tmp_path / "prompt.md"),
        "lane_guard": {"claim_snapshot": current, "pid": 10},
    }
    with pytest.raises(ValueError, match="live"):
        redirect_plan.apply_plan(plan, confirm_target="o/r#1", pid_checker=lambda _: True)
    assert not (tmp_path / "prompt.md").exists()


def test_same_dead_claim_can_release(tmp_path, monkeypatch):
    current = {"target": "o/r#1", "agent": "codex", "pid": 10, "ts": 1}
    monkeypatch.setattr(redirect_plan.claims, "holder", lambda _: current)
    monkeypatch.setattr(redirect_plan.capabilities, "production_heartbeat", lambda *a, **k: None)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    plan = {
        "action": "redirect",
        "target": "o/r#1",
        "prompt_text": "retry",
        "prompt_file": str(tmp_path / "prompt.md"),
        "lane_guard": {"claim_snapshot": current, "pid": 10},
        "steps": [{"id": "release-claim", "commands": [["release", "o/r#1"]]}],
    }
    result = redirect_plan.apply_plan(
        plan, confirm_target="o/r#1", runner=runner, pid_checker=lambda _: False
    )
    assert result["applied"]
    assert calls == [["release", "o/r#1"]]


def test_claim_replaced_at_release_boundary_is_refused(tmp_path, monkeypatch):
    old = {"target": "o/r#1", "agent": "codex", "pid": 10, "ts": 1}
    replacement = dict(old, ts=2, pid=20)
    reads = iter([old, replacement])
    monkeypatch.setattr(redirect_plan.claims, "holder", lambda _: next(reads))
    monkeypatch.setattr(redirect_plan.capabilities, "production_heartbeat", lambda *a, **k: None)
    calls = []
    plan = {
        "action": "redirect",
        "target": "o/r#1",
        "prompt_text": "retry",
        "prompt_file": str(tmp_path / "prompt.md"),
        "lane_guard": {"claim_snapshot": old, "pid": 10},
        "steps": [{"id": "release-claim", "commands": [["release", "o/r#1"]]}],
    }
    with pytest.raises(ValueError, match="identity changed"):
        redirect_plan.apply_plan(
            plan,
            confirm_target="o/r#1",
            runner=lambda *a, **k: calls.append(a),
            pid_checker=lambda _: False,
        )
    assert calls == []


def test_claim_missing_snapshot_is_not_authority(tmp_path, monkeypatch):
    current = {"target": "o/r#1", "agent": "codex", "pid": 10, "ts": 1}
    monkeypatch.setattr(redirect_plan.claims, "holder", lambda _: current)
    monkeypatch.setattr(redirect_plan.capabilities, "production_heartbeat", lambda *a, **k: None)
    plan = {
        "action": "redirect",
        "target": "o/r#1",
        "prompt_text": "retry",
        "prompt_file": str(tmp_path / "prompt.md"),
        "lane_guard": {"claim_snapshot": None, "pid": 10},
    }
    with pytest.raises(ValueError, match="identity changed"):
        redirect_plan.apply_plan(plan, confirm_target="o/r#1", pid_checker=lambda _: False)


def test_reclaimed_target_refuses_delegation(tmp_path, monkeypatch):
    replacement = {"target": "o/r#1", "agent": "codex", "pid": 20, "ts": 2}
    reads = iter([None, replacement])
    monkeypatch.setattr(redirect_plan.claims, "holder", lambda _: next(reads))
    monkeypatch.setattr(redirect_plan.capabilities, "production_heartbeat", lambda *a, **k: None)
    calls = []
    plan = {
        "action": "redirect",
        "target": "o/r#1",
        "prompt_text": "retry",
        "prompt_file": str(tmp_path / "prompt.md"),
        "lane_guard": {"claim_snapshot": None, "pid": 10},
        "steps": [{"id": "delegate-retry", "commands": [["delegate", "o/r#1"]]}],
    }
    with pytest.raises(ValueError, match="reclaimed"):
        redirect_plan.apply_plan(
            plan,
            confirm_target="o/r#1",
            runner=lambda *a, **k: calls.append(a),
            pid_checker=lambda _: False,
        )
    assert calls == []
