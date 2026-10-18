"""An active CLI tick must launch research while remote dispatch stays off."""

import json

import exp_abcd
import experiment_producer as producer
import tick


def test_producer_launches_with_dispatch_lane_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_DISPATCH_LANE", "0")
    monkeypatch.setenv("ORCH_EXPERIMENT_PRODUCER", "1")
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "0")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tick.router, "load_capacity", lambda: {})
    monkeypatch.setattr(tick.router, "load_backlog", lambda: [])
    monkeypatch.setattr(tick.router, "learned_ranks", lambda: {})
    monkeypatch.setattr(tick, "load_research_feed", lambda: {})
    monkeypatch.setattr(tick, "backlog_age", lambda: {})
    monkeypatch.setattr(tick.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(tick.claims, "active_claims", lambda: {})
    monkeypatch.setattr(tick.claims, "claim", lambda *a: True)
    monkeypatch.setattr(tick.claims, "release", lambda *a: None)
    monkeypatch.setattr(tick.capabilities, "production_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(tick.research_subjects, "assess_candidate", lambda **k: {"eligible": True})
    monkeypatch.setattr(tick.research_subjects, "record_subject", lambda *a, **k: None)
    monkeypatch.setattr(tick.research_subjects, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(
        tick.research_scheduler,
        "build_research_plan",
        lambda *a, **k: {"planned": [], "skipped": []},
    )
    job = {
        "item": {"target": "a/b#3"},
        "arms": ["codex", "claude"],
        "pools": {"codex": "ok", "claude": "ok"},
    }
    monkeypatch.setattr(
        producer, "build_plan", lambda *a, **k: {"planned": [job], "skipped": [], "candidates": []}
    )
    monkeypatch.setattr(producer, "cadence_draw", lambda *a, **k: {"fire": True})
    monkeypatch.setattr(producer, "issue_status", lambda *a, **k: {"stdout": ""})
    monkeypatch.setattr(tick, "_fetch_issue_body", lambda *a, **k: "Frozen issue specification")
    monkeypatch.setattr(exp_abcd, "exp_paths", lambda exp_id: tmp_path / exp_id)
    prepared = []
    monkeypatch.setattr(
        exp_abcd,
        "prepare_arms",
        lambda *a, **k: prepared.append(a) or {"launched": [], "base_sha": "base"},
    )
    original = tick.remote_tick

    def remote(items, cap, **kwargs):
        assert kwargs["dry_run"] is True
        assert kwargs["ingest_dry_run"] is False
        # Exercise the real remote_tick research seam, without unrelated dispatch/ingest I/O.
        return original(items, cap, **kwargs, do_ingest=False, env=dict(tick.os.environ))

    monkeypatch.setattr(tick, "remote_tick", remote)
    assert tick.main(["--active"]) == 0
    assert len(prepared) == 1
    metas = list(tmp_path.glob("*/meta.json"))
    assert json.loads(metas[0].read_text())["producer"] == "experiment_producer"


def test_nonproducer_retains_legacy_research_switch_semantics(monkeypatch):
    monkeypatch.setenv("ORCH_DISPATCH_LANE", "1")
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")
    monkeypatch.setenv("ORCH_EXPERIMENT_PRODUCER", "0")
    monkeypatch.setattr(tick.router, "load_capacity", lambda: {})
    monkeypatch.setattr(tick.router, "load_backlog", lambda: [])
    monkeypatch.setattr(tick.router, "learned_ranks", lambda: {})
    monkeypatch.setattr(tick, "load_research_feed", lambda: {})
    monkeypatch.setattr(tick, "backlog_age", lambda: {})
    observed = []
    monkeypatch.setattr(tick, "remote_tick", lambda *a, **k: observed.append(k) or {})
    tick.main(["--active"])
    assert observed[0]["research_dry_run"] is False
